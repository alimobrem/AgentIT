"""Shared helpers used by app.py and route modules."""
from __future__ import annotations

import logging
import os
import time as _time
from urllib.parse import urlparse

log = logging.getLogger(__name__)


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


# ── Store singleton ───────────────────────────────────────────────────

from agentit.portal.store import AssessmentStore  # noqa: E402

_store = AssessmentStore()


def get_store() -> AssessmentStore:
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
    if parsed.scheme not in ("https", "http", ""):
        return "#"
    return value


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

import asyncio  # noqa: E402

OPERATION_TIMEOUT = 300


async def with_timeout(coro, timeout: int = OPERATION_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        from fastapi import HTTPException
        raise HTTPException(504, f"Operation timed out after {timeout}s")


# ── Clone + assess + cleanup ─────────────────────────────────────────

import shutil  # noqa: E402


def clone_assess_cleanup(repo_url: str, criticality: str, infra_repo_url: str | None = None):
    """Clone a repo, run assessment, clean up the clone."""
    from agentit.cloner import clone_repo
    from agentit.runner import run_assessment
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=get_llm_client(), infra_repo_url=infra_repo_url,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


# ── Run onboarding ───────────────────────────────────────────────────


def run_onboarding(report, assessment_id: str | None = None):
    """Run orchestrated onboarding. Returns (files, orchestration_summary)."""
    import tempfile
    from pathlib import Path
    from agentit.agents.orchestrator import FleetOrchestrator

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        orch = FleetOrchestrator(
            report=report, output_dir=base,
            store=get_store(), assessment_id=assessment_id,
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
                {"name": ar.agent_name, "category": ar.category,
                 "success": ar.success, "files": ar.files_generated,
                 "error": ar.error}
                for ar in result.agent_results
            ],
            "conflicts": result.conflicts,
            "recommendation": result.recommendation,
        }

        return all_files, orch_summary
