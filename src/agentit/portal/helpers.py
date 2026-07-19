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


# ── Credential health ─────────────────────────────────────────────────
#
# The 3 credentials that genuinely require admin action to configure, and
# that every GitHub- or LLM-dependent feature silently degrades without:
# the GitHub token, the GitHub webhook HMAC secret, and the LLM backend
# (Vertex AI service-account credentials or a direct Anthropic API key).
# get_credential_states() mirrors get_circuit_breaker_states()'s shape
# above so the Health page's Credentials table renders identically to its
# Circuit Breakers table.


def _check_llm_backend() -> dict[str, object]:
    """Report which LLM backend `agentit.llm._create_client()` would
    actually select, checking the same env vars it does.

    Vertex AI is confirmed only via existence + readability of the
    `GOOGLE_APPLICATION_CREDENTIALS` file -- a full GCP auth round trip is
    too expensive/complex for a Health-page check, so this is a cheap,
    reasonable proxy for "the mounted service-account key is usable".
    """
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "")
    if project and region:
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not creds_path:
            return {
                "ok": False, "status": "invalid",
                "detail": (
                    "Vertex AI selected (ANTHROPIC_VERTEX_PROJECT_ID + CLOUD_ML_REGION "
                    "set) but GOOGLE_APPLICATION_CREDENTIALS is not set"
                ),
            }
        if not os.path.isfile(creds_path):
            return {
                "ok": False, "status": "invalid",
                "detail": f"GOOGLE_APPLICATION_CREDENTIALS ({creds_path}) does not exist",
            }
        if not os.access(creds_path, os.R_OK):
            return {
                "ok": False, "status": "invalid",
                "detail": f"GOOGLE_APPLICATION_CREDENTIALS ({creds_path}) exists but is not readable",
            }
        return {
            "ok": True, "status": "valid",
            "detail": f"Vertex AI backend (project={project}, region={region})",
        }
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": True, "status": "valid", "detail": "Direct Anthropic API backend (ANTHROPIC_API_KEY set)"}
    return {
        "ok": False, "status": "missing",
        "detail": (
            "No LLM backend configured -- neither Vertex AI "
            "(ANTHROPIC_VERTEX_PROJECT_ID + CLOUD_ML_REGION) nor ANTHROPIC_API_KEY are set"
        ),
    }


def get_credential_states() -> dict[str, dict[str, object]]:
    """Live status for the GitHub token, GitHub webhook secret, and LLM
    backend credentials -- used by the Health page's Credentials table.
    """
    from agentit.portal import github_pr

    github_check = github_pr.check_github_token()
    webhook_secret_set = bool(os.environ.get("GITHUB_WEBHOOK_SECRET"))

    return {
        "github-token": {
            "ok": github_check["status"] == "valid",
            "status": github_check["status"],
            "detail": github_check["detail"],
        },
        "github-webhook-secret": {
            "ok": webhook_secret_set,
            "status": "configured" if webhook_secret_set else "missing",
            "detail": (
                "GITHUB_WEBHOOK_SECRET is set" if webhook_secret_set
                else "GITHUB_WEBHOOK_SECRET is not set -- inbound webhook signatures cannot be verified"
            ),
        },
        "llm-backend": _check_llm_backend(),
    }


# ── Store singleton ───────────────────────────────────────────────────
#
# ``get_store()`` is async -- it's the only way any caller (portal, CLI,
# watchers) ever gets an ``AssessmentStore``. The singleton is created
# lazily (not at import time) since pool creation is itself async; a lock
# guards against two concurrent requests both racing to create it on first
# use. Sized for the portal's concurrency profile (min_size=5, max_size=20)
# -- see docs/postgres-migration-plan.md for the historical pool-sizing
# rationale per component.

_store: object | None = None
_store_lock = asyncio.Lock()


async def get_store():
    global _store
    if _store is None:
        async with _store_lock:
            if _store is None:
                from agentit.portal.store import create_store
                _store = await create_store(min_size=5, max_size=20)
    return _store


# ── Nav gate badges ───────────────────────────────────────────────────
#
# base.html's nav bar shows the fleet-wide count of PRs waiting for your
# approval on Ledger's link -- a pending `gitops-pr-pending`/
# `gitops-pr-pending-shared-namespace` gate, the only gate types Ledger's
# PR-lifecycle view renders/acts on, now that Ledger is strictly PR-focused
# rather than every app-owner-scoped gate type (docs/ui-redesign-proposal.md
# §2/§5). Every other app-owner gate type (`rollback-review`,
# `finding-unresolved-escalation`, ...) stays visible via Fleet's per-app
# "needs action"/escalation badges and Assessment Detail's Actions tab, not
# this nav badge. This also closes a real pre-existing defect: the old nav
# referenced a `pending_gates` template variable no context processor ever
# actually supplied (only Insights' own page context computed it, so the
# badge was silently blank everywhere else). Cached briefly since nav
# renders on every page.
#
# Used to also split out a separate `admin_review` count for a second,
# cross-app "Admin Review" nav link -- retired 2026-07-18 along with the
# `cluster-admin-review` gate type it existed solely for.

_nav_gate_badges_cache: dict = {"pending_actions": 0, "ts": 0.0}
_NAV_GATE_BADGES_CACHE_TTL = 20  # seconds
# Double-checked locking, mirroring get_store() above: the `await
# store.list_gates(...)` below is a genuine yield point, so without a lock,
# multiple concurrent requests can all see a stale cache, all refresh, and
# interleave their writes into a torn read for a third caller. This is
# async-only (never invoked via asyncio.to_thread), so an `asyncio.Lock` --
# not a `threading.Lock` -- is the correct primitive here.
_nav_gate_badges_lock = asyncio.Lock()


async def get_nav_gate_badge_counts(store: object) -> dict[str, int]:
    now = _time.monotonic()
    if now - _nav_gate_badges_cache["ts"] < _NAV_GATE_BADGES_CACHE_TTL:
        return {"pending_actions": _nav_gate_badges_cache["pending_actions"]}
    async with _nav_gate_badges_lock:
        now = _time.monotonic()
        if now - _nav_gate_badges_cache["ts"] < _NAV_GATE_BADGES_CACHE_TTL:
            return {"pending_actions": _nav_gate_badges_cache["pending_actions"]}
        try:
            gates = await store.list_gates(status="pending")
        except Exception:
            log.debug("Failed to refresh nav gate badge counts", exc_info=True)
            gates = []

        from agentit.portal.delivery import _CICD_SHARED_NAMESPACE_GATE_TYPE
        pending_actions = sum(
            1 for g in gates
            if g.get("gate_type") in ("gitops-pr-pending", _CICD_SHARED_NAMESPACE_GATE_TYPE)
        )

        _nav_gate_badges_cache["pending_actions"] = pending_actions
        _nav_gate_badges_cache["ts"] = now
        return {"pending_actions": pending_actions}


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


def is_authenticated(request) -> bool:
    """True only when a real oauth-proxy sidecar sits in front of this
    request (i.e. `X-Forwarded-User` is actually present) -- meaning there's
    a real session to sign out of and a real identity to show, not just the
    "portal-user" fallback.

    Deliberately a runtime, per-request signal rather than a static
    `auth.enabled` check baked into the template: the chart flag only
    controls whether the oauth-proxy *container* exists in the Deployment --
    the exact same rendered `base.html`/image is served whether it's true or
    false, so the nav bar's Logout link / "Logged in as" text (base.html)
    must key off something that's actually true of *this* request.
    """
    return bool(request.headers.get("X-Forwarded-User"))


# openshift/oauth-proxy (the fork `ose-oauth-proxy` builds from -- see
# https://github.com/openshift/oauth-proxy) exposes its sign-out endpoint at
# `<proxy-prefix>/sign_out`, where `--proxy-prefix` defaults to "/oauth" when
# not overridden. chart/templates/deployment.yaml's oauth-proxy args don't
# set `--proxy-prefix`, so the default applies -- see this module's
# test_helpers.py, which parses that file's args and fails loudly if someone
# adds an override without updating this constant to match. Note this
# specific fork's `/oauth/sign_out` handler ignores any `?rd=` query param
# (that's an oauth2-proxy-only feature) and always redirects to `/` after
# clearing the session cookie, which for this app is the Fleet page -- a
# fine landing spot, so no redirect param is needed here.
OAUTH_PROXY_SIGN_OUT_PATH = "/oauth/sign_out"


DIMENSION_LABELS: dict[str, str] = {
    "ha_dr": "HA/DR",
    "cicd": "CI/CD",
    "data_governance": "Data Governance",
    # Delivery/PR categories (delivery.py's CATEGORY_* constants) reuse this
    # same filter for Ledger's category filter/badges (routes/insights.py::
    # ledger_page(), templates/ledger.html) -- not just assessment
    # dimensions -- so acronym-bearing category names need the same
    # explicit mapping a bare Title Case fallback would get wrong.
    "cicd_shared_namespace": "CI/CD (shared namespace)",
    "cluster_config": "Cluster config",
    "source_patch": "Source patch",
    "manifest_at_rest": "Manifest at rest",
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
    secret_decisions_out: list[dict] | None = None,
):
    """Clone a repo, run assessment, clean up the clone.

    ``check_results_out``, if given, is populated with per-check pass/fail
    rows the caller can persist via ``store.save_check_results`` once it has
    an ``assessment_id`` (see ``AssessmentStore.save_check_results``).

    ``secret_decisions_out``, if given, is populated with the security
    analyzer's real `classify_secret` verdicts the caller can persist via
    ``llm_decisions.build_secret_classify_events()`` + ``store.log_event()``
    once it has an ``assessment_id``/repo name (see ``runner.run_assessment``).
    """
    from agentit.cloner import clone_repo
    from agentit.runner import run_assessment
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=get_llm_client(), infra_repo_url=infra_repo_url,
            check_results_out=check_results_out,
            secret_decisions_out=secret_decisions_out,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


# ── Run onboarding ───────────────────────────────────────────────────


async def run_onboarding(report, assessment_id: str | None = None, store: object | None = None):
    """Run orchestrated onboarding. Returns (files, orchestration_summary).

    This is the single shared implementation used by both the inline portal
    route (app.py) and the webhook-triggered path (routes/webhooks.py) — keep
    the orchestration summary fields in sync between callers since
    `auto_approve`/`gates` are read downstream (e.g. webhook_auto_apply).

    ``store`` must be an async-compatible store (e.g. what ``await
    get_store()`` returns) -- ``FleetOrchestrator`` is now genuinely
    ``async def`` throughout, so this function is itself a coroutine,
    `await`ed directly by its callers with no more ``asyncio.to_thread``
    bridge needed for this specific call path. Falls back to this module's
    own singleton (``await get_store()``) only if a caller omits `store`
    entirely.
    """
    import tempfile
    from pathlib import Path
    from agentit.agents.orchestrator import FleetOrchestrator

    if store is None:
        store = await get_store()

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        orch = FleetOrchestrator(
            report=report, output_dir=base,
            store=store, assessment_id=assessment_id,
        )
        result = await orch.run()

        all_files: list[dict] = []
        for ar in result.agent_results:
            if not ar.success:
                continue
            category_dir = base / ar.category
            # See agents/orchestrator.py::_write_target_path_manifest -- the
            # only way CodeChangeAgent's real target file (e.g. "Dockerfile")
            # survives from the agent's in-memory GeneratedFile.target_path
            # to this dict, which is all the delivery router downstream ever
            # sees.
            target_paths: dict[str, str] = {}
            manifest_path = category_dir / "_target_paths.json"
            if manifest_path.is_file():
                import json as _json
                try:
                    target_paths = _json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    log.warning("Failed to read target-path manifest for %s", ar.category, exc_info=True)
            # Real per-file intent (why this file exists) -- see
            # agents/orchestrator.py::_write_file_metadata_manifest(). Falls
            # back to the bare relative path (the old behavior) only when
            # the sidecar is missing/unreadable, never a fabricated "why".
            file_metadata: dict[str, dict] = {}
            metadata_path = category_dir / "_file_metadata.json"
            if metadata_path.is_file():
                import json as _json
                try:
                    file_metadata = _json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    log.warning("Failed to read file-metadata manifest for %s", ar.category, exc_info=True)
            for rel_path in ar.files_generated:
                file_path = category_dir / rel_path
                if file_path.is_file():
                    meta = file_metadata.get(rel_path, {})
                    entry = {
                        "category": ar.category,
                        "path": rel_path,
                        "description": meta.get("description") or rel_path,
                        "content": file_path.read_text(encoding="utf-8"),
                    }
                    if meta.get("finding_addressed"):
                        entry["finding_addressed"] = meta["finding_addressed"]
                    if meta.get("skill_name"):
                        entry["skill_name"] = meta["skill_name"]
                    target_path = target_paths.get(rel_path, "")
                    if target_path:
                        entry["target_path"] = target_path
                    all_files.append(entry)

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
