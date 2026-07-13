"""Shared helpers used by app.py and route modules."""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def get_retention_days() -> int:
    return int(os.environ.get("AGENTIT_RETENTION_DAYS", "30"))


# ── Circuit breaker ──────────────────────────────────────────────────


class CircuitBreaker:
    """Simple circuit breaker: opens after threshold failures, resets after reset_after seconds."""

    def __init__(self, name: str, threshold: int = 3, reset_after: float = 30.0):
        self.name = name
        self._threshold = threshold
        self._reset_after = reset_after
        self._failures = 0
        self._last_failure: float = 0

    @property
    def is_open(self) -> bool:
        if self._failures < self._threshold:
            return False
        return (_time.monotonic() - self._last_failure) < self._reset_after

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure = _time.monotonic()

    def record_success(self) -> None:
        self._failures = 0

    def __repr__(self) -> str:
        state = "OPEN" if self.is_open else "CLOSED"
        return f"CircuitBreaker({self.name}, {state}, failures={self._failures})"


llm_breaker = CircuitBreaker("llm", threshold=3, reset_after=30)
kube_breaker = CircuitBreaker("kube", threshold=5, reset_after=60)

_ALL_BREAKERS: dict[str, CircuitBreaker] = {"llm": llm_breaker, "kube": kube_breaker}


def get_circuit_breaker_states() -> dict[str, dict[str, object]]:
    """Expose current open/closed state for every registered circuit breaker.

    Used by `/health`, the Prometheus `agentit_circuit_breaker_open` gauge,
    and the Health page — a single accessor so all three stay in sync.
    """
    return {
        name: {"open": breaker.is_open, "failures": breaker._failures}
        for name, breaker in _ALL_BREAKERS.items()
    }


# ── Store singleton ───────────────────────────────────────────────────
#
# ``get_store()`` is async -- Phase 3 of docs/postgres-migration-plan.md.
# The singleton is created lazily (not at import time) since pool/connection
# setup is itself async; a lock guards against two concurrent requests both
# racing to create it on first use. Sized per the plan's §5 Portal row
# (min_size=5, max_size=20) -- only meaningful once AGENTIT_DB_BACKEND is
# ever flipped to "postgres" (still unset/"sqlite" everywhere today; see
# store_factory.py and the plan's §7 for why that switch must stay a single,
# coordinated, all-components-at-once cutover rather than happening here).

_store: object | None = None
_store_lock = asyncio.Lock()


async def get_store():
    global _store
    if _store is None:
        async with _store_lock:
            if _store is None:
                from agentit.portal.store_factory import create_store
                _store = await create_store(min_size=5, max_size=20)
    return _store


# ── Event publishing ──────────────────────────────────────────────────


def publish_event(
    action: str,
    target_app: str | None,
    summary: str,
    details: dict | None = None,
    correlation_id: str | None = None,
    agent_id: str = "assessor",
    extra_topic: str | None = None,
) -> None:
    try:
        from agentit.events import get_publisher, TOPIC_EVENTS
        pub = get_publisher()
        kwargs = dict(
            agent_id=agent_id,
            action=action,
            target_app=target_app,
            summary=summary,
            details=details,
            correlation_id=correlation_id,
        )
        pub.publish(TOPIC_EVENTS, **kwargs)
        if extra_topic:
            pub.publish(extra_topic, **kwargs)
    except Exception:
        log.warning("Failed to publish event %s", action, exc_info=True)


# ── Display helpers ───────────────────────────────────────────────────


def safe_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http"):
        return "#"
    return value


def get_current_user(request) -> str:
    """Resolve the identity of the browser user making this request.

    When `auth.enabled` (chart/templates/deployment.yaml), the oauth-proxy
    sidecar authenticates the user against the cluster's OAuth server and
    forwards their username via `X-Forwarded-User` -- the app trusts this
    header as-is rather than re-verifying it, since the proxy is a sidecar in
    the same pod (reached over loopback), not a separate network hop an
    external attacker could spoof; NetworkPolicy still restricts who can
    reach the app's real port directly (see networkpolicy.yaml).

    Falls back to "portal-user" when the header is absent -- the default in
    local dev, tests, and any deployment with `auth.enabled=false`, so this
    never breaks environments without the proxy in front of them.
    """
    return request.headers.get("X-Forwarded-User") or "portal-user"


DIMENSION_LABELS: dict[str, str] = {
    "ha_dr": "HA/DR",
    "cicd": "CI/CD",
    "data_governance": "Data Governance",
}


def format_dimension(value: str) -> str:
    """Format dimension names for display. Uses explicit mapping for acronyms,
    falls back to replacing underscores and title-casing."""
    if value in DIMENSION_LABELS:
        return DIMENSION_LABELS[value]
    return value.replace("_", " ").title()


# ── LLM client ────────────────────────────────────────────────────────


def get_llm_client():
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        try:
            from agentit.llm import LLMClient
            return LLMClient()
        except Exception as exc:
            log.warning("LLM client init failed (continuing without): %s", exc)
    return None


# ── Templates singleton ──────────────────────────────────────────────

_templates = None


def get_templates():
    """Lazy-load Jinja2 templates from the portal app."""
    global _templates
    if _templates is None:
        from agentit.portal.app import templates
        _templates = templates
    return _templates


# ── Async timeout wrapper ────────────────────────────────────────────

OPERATION_TIMEOUT = 300


async def with_timeout(coro, timeout: int = OPERATION_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        from fastapi import HTTPException
        raise HTTPException(504, f"Operation timed out after {timeout}s")


# ── Clone + assess + cleanup ─────────────────────────────────────────

import shutil  # noqa: E402


def clone_assess_cleanup(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
):
    """Clone a repo, run assessment, clean up the clone.

    ``check_results_out``, if given, is populated with per-check pass/fail
    rows the caller can persist via ``store.save_check_results`` once it has
    an ``assessment_id`` (see ``AssessmentStore.save_check_results``).
    """
    from agentit.cloner import clone_repo
    from agentit.runner import run_assessment
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=get_llm_client(), infra_repo_url=infra_repo_url,
            check_results_out=check_results_out,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


# ── Run onboarding ───────────────────────────────────────────────────


def run_onboarding(report, assessment_id: str | None = None, store: object | None = None):
    """Run orchestrated onboarding. Returns (files, orchestration_summary).

    This is the single shared implementation used by both the inline portal
    route (app.py) and the webhook-triggered path (routes/webhooks.py) — keep
    the orchestration summary fields in sync between callers since
    `auto_approve`/`gates` are read downstream (e.g. webhook_auto_apply).

    ``store`` must be a *synchronous* store (e.g. ``(await get_store()).raw``)
    -- ``FleetOrchestrator`` (used below) is deliberately still fully
    synchronous (see docs/postgres-migration-plan.md's Phase 3 progress
    notes), and every real call site already runs this function inside a
    worker thread via ``asyncio.to_thread``, so it cannot itself ``await``
    the async store singleton. Callers (namely app.py's `_run_onboarding`
    and webhooks.py's `webhook_onboard`) should resolve `get_store()` in
    their own async context first and pass `.raw` explicitly. Falls back to
    this module's own singleton's `.raw` (or a fresh sync store) only if a
    caller omits `store` entirely.
    """
    import tempfile
    from pathlib import Path
    from agentit.agents.orchestrator import FleetOrchestrator

    if store is None:
        if _store is not None and hasattr(_store, "raw"):
            store = _store.raw
        else:
            from agentit.portal.store import AssessmentStore
            store = AssessmentStore()

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        orch = FleetOrchestrator(
            report=report, output_dir=base,
            store=store, assessment_id=assessment_id,
        )
        result = orch.run()

        all_files: list[dict] = []
        for ar in result.agent_results:
            if not ar.success:
                continue
            category_dir = base / ar.category
            for rel_path in ar.files_generated:
                file_path = category_dir / rel_path
                if file_path.is_file():
                    all_files.append({
                        "category": ar.category,
                        "path": rel_path,
                        "description": rel_path,
                        "content": file_path.read_text(encoding="utf-8"),
                    })

        orch_summary = {
            "agents": [
                {
                    "name": ar.agent_name,
                    "category": ar.category,
                    "success": ar.success,
                    "files_count": len(ar.files_generated),
                    "error": ar.error,
                }
                for ar in result.agent_results
            ],
            "conflicts": result.conflicts,
            "recommendation": result.recommendation,
            "auto_approve": result.plan.auto_approve,
            "gates": result.gates_created,
        }

        return all_files, orch_summary
