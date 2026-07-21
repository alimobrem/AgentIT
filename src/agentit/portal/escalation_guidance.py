"""Enrich Ledger escalation cards with real "why we failed" / "how to fix
manually" guidance — never invent failure detail.

Used by Assessment Detail's Ledger tab (``recommendation_card``) for
``finding-escalated`` events. Failure reasons come only from stored events /
delivery outcomes; manual guidance prefers the assessment finding's own
``recommendation``, then skill/check metadata, then a small category map.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from agentit.portal.delivery import FINDING_ESCALATION_THRESHOLD, _escalation_event_category

logger = logging.getLogger("agentit.portal.escalation_guidance")

_ATTEMPTS_RE = re.compile(r"failed to resolve after (\d+)\s+automated fix", re.I)
_TARGET_FINDING_RE = re.compile(r"Target finding:\s*(.+?)\s*$", re.I)

# Honest fallbacks — never invent a specific root cause.
_MISSING_WHY = (
    "No failure detail recorded — check Events for this assessment."
)
_MISSING_HOW = (
    "No specific manual-fix guidance recorded for this finding — "
    "see the Findings tab and Events for context."
)

# Finding-specific manual steps when recommendation/skill metadata is absent.
# Prefer real skill/finding text over these; keep short and concrete.
_MANUAL_FIX_BY_CATEGORY: dict[str, str] = {
    "network": (
        "Add a Kubernetes NetworkPolicy that restricts ingress/egress for "
        "this app's pods (deny-by-default, allow only required peers)."
    ),
    "autoscaling": (
        "Add a HorizontalPodAutoscaler targeting the Deployment so replicas "
        "scale with load."
    ),
    "scaling": (
        "Add a HorizontalPodAutoscaler targeting the Deployment so replicas "
        "scale with load."
    ),
    "dockerfile": (
        "Pin the base image to a digest or specific version in the "
        "Containerfile/Dockerfile — avoid the `:latest` tag."
    ),
    "container": (
        "Pin the base image to a digest or specific version in the "
        "Containerfile/Dockerfile — avoid the `:latest` tag."
    ),
    "base_image": (
        "Pin the base image to a digest or specific version in the "
        "Containerfile/Dockerfile — avoid the `:latest` tag."
    ),
    "resource": (
        "Set CPU/memory requests and limits on the Deployment's containers."
    ),
    "rbac": (
        "Add a least-privilege ServiceAccount, Role, and RoleBinding for the "
        "workload (avoid cluster-admin)."
    ),
    "pipeline": (
        "Add a CI pipeline (e.g. Tekton or GitHub Actions) that builds, "
        "tests, and publishes this app."
    ),
    "gitops": (
        "Ensure desired state lives in git and is synced by Argo CD — open "
        "or merge the delivery PR rather than applying by hand."
    ),
    "metrics": (
        "Expose a Prometheus metrics endpoint and add a ServiceMonitor (or "
        "equivalent) so the app is scraped."
    ),
    "monitoring": (
        "Expose a Prometheus metrics endpoint and add a ServiceMonitor (or "
        "equivalent) so the app is scraped."
    ),
    "sbom": (
        "Add an SBOM generation step to the build pipeline (e.g. syft/cdxgen)."
    ),
    "migration": (
        "Add database migration tooling (e.g. migrate, flyway, or alembic) "
        "and run migrations as part of deploy."
    ),
    "pdb": (
        "Add a PodDisruptionBudget so voluntary disruptions leave enough "
        "healthy replicas."
    ),
    "replicas": (
        "Raise Deployment replicas (and add HPA/PDB as needed) for high "
        "availability."
    ),
}

_WHY_EVENT_ACTIONS = (
    "finding-redispatch-no-fix",
    "delivery-finding-still-present",
    "finding-redispatched",
    "auto-delivery-failed",
    "auto-validation-failed",
)


def parse_escalation_summary(summary: str) -> dict[str, Any]:
    """Pull category / attempt count / target finding text from the
    deterministic summary ``escalate_unresolved_finding()`` writes."""
    summary = summary or ""
    category = _escalation_event_category(summary)
    attempts_match = _ATTEMPTS_RE.search(summary)
    target_match = _TARGET_FINDING_RE.search(summary)
    return {
        "category": category,
        "attempt_count": int(attempts_match.group(1)) if attempts_match else FINDING_ESCALATION_THRESHOLD,
        "finding_title": (target_match.group(1).strip() if target_match else "") or category,
    }


def _manual_fix_for_category(category: str) -> str | None:
    cat = (category or "").lower().replace(" ", "_").replace("-", "_")
    if cat in _MANUAL_FIX_BY_CATEGORY:
        return _MANUAL_FIX_BY_CATEGORY[cat]
    for key, guidance in _MANUAL_FIX_BY_CATEGORY.items():
        if key in cat:
            return guidance
    return None


def _finding_match(report: Any, category: str, finding_title: str) -> Any | None:
    """Best matching Finding on the assessment report for this escalation."""
    if report is None:
        return None
    cat = (category or "").lower()
    title = (finding_title or "").lower()
    best = None
    for score in getattr(report, "scores", []) or []:
        for finding in getattr(score, "findings", []) or []:
            fcat = (getattr(finding, "category", "") or "").lower()
            if fcat != cat and cat not in fcat and fcat not in cat:
                continue
            desc = (getattr(finding, "description", "") or "").lower()
            if title and (title in desc or desc in title or title[:40] in desc):
                return finding
            if best is None:
                best = finding
    return best


def _dimension_for_finding(report: Any, finding: Any, category: str) -> str:
    if finding is not None and report is not None:
        for score in getattr(report, "scores", []) or []:
            if finding in getattr(score, "findings", []) or []:
                return getattr(score, "dimension", "") or category
    if report is not None:
        cat = (category or "").lower()
        for score in getattr(report, "scores", []) or []:
            for f in getattr(score, "findings", []) or []:
                fcat = (getattr(f, "category", "") or "").lower()
                if fcat == cat or cat in fcat or fcat in cat:
                    return getattr(score, "dimension", "") or category
    return category


def _skill_recommendation(category: str) -> str | None:
    """Prefer a registered skill's front-matter recommendation when present."""
    try:
        from agentit.remediation.dispatcher import _default_skills_dir
        from agentit.skill_engine import SkillEngine
    except Exception:
        logger.debug("Skill lookup unavailable for escalation guidance", exc_info=True)
        return None
    try:
        engine = SkillEngine(_default_skills_dir(), platform=None)
        skill = engine.skill_for_category(category)
    except Exception:
        logger.debug("Failed loading skill for category %s", category, exc_info=True)
        return None
    if skill is None:
        return None
    rec = (getattr(skill, "recommendation", "") or "").strip()
    return rec or None


def _why_from_events(events: list[dict], category: str) -> str | None:
    cat = (category or "").lower()
    for event in events:
        if event.get("action") not in _WHY_EVENT_ACTIONS:
            continue
        summary = (event.get("summary") or "").strip()
        if not summary:
            continue
        summary_l = summary.lower()
        if cat and f"'{cat}'" not in summary_l and cat not in summary_l:
            continue
        return summary
    return None


async def enrich_escalation_event(
    store: object,
    event: dict,
    report: Any | None = None,
) -> dict:
    """Attach display fields for Ledger escalation cards.

    Adds: ``finding_title``, ``category``, ``dimension``, ``attempt_count``,
    ``why_failed``, ``how_to_fix_manually``. Mutates and returns ``event``.
    """
    parsed = parse_escalation_summary(event.get("summary") or "")
    category = parsed["category"]
    finding_title = parsed["finding_title"]
    attempt_count = parsed["attempt_count"]

    app_name = event.get("target_app") or getattr(report, "repo_name", "") or ""
    if hasattr(store, "get_finding_failure_count") and app_name and category:
        try:
            stored_count = await store.get_finding_failure_count(app_name, category)
            if stored_count and stored_count > 0:
                attempt_count = stored_count
        except Exception:
            logger.debug("failure-count lookup failed for %s/%s", app_name, category, exc_info=True)

    finding = _finding_match(report, category, finding_title)
    if finding is not None and getattr(finding, "description", None):
        finding_title = finding.description
    dimension = _dimension_for_finding(report, finding, category)

    why_failed = None
    if app_name and hasattr(store, "list_events"):
        try:
            recent = await store.list_events(target_app=app_name, limit=80)
            why_failed = _why_from_events(recent, category)
        except Exception:
            logger.debug("event scan failed for escalation why on %s", app_name, exc_info=True)

    how = None
    if finding is not None:
        how = (getattr(finding, "recommendation", "") or "").strip() or None
    if not how:
        how = _skill_recommendation(category)
    if not how:
        how = _manual_fix_for_category(category)

    event["finding_title"] = finding_title
    event["category"] = category
    event["dimension"] = dimension
    event["attempt_count"] = attempt_count
    event["why_failed"] = why_failed or _MISSING_WHY
    event["how_to_fix_manually"] = how or _MISSING_HOW
    event["why_failed_recorded"] = bool(why_failed)
    return event


async def enrich_escalations(
    store: object,
    escalations: list[dict],
    report: Any | None = None,
) -> list[dict]:
    """Enrich every escalation event in place."""
    for event in escalations:
        await enrich_escalation_event(store, event, report)
    return escalations
