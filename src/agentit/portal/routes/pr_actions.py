"""Real, direct actions on a GitHub PR AgentIT opened -- Merge/Close --
replacing the generic gates system's ``gitops-pr-pending``/``-shared-
namespace`` approval step. Every delivery category (GitOps infra-repo
commit, source-repo-pr, app-repo-pr, onboarding) now gets the exact same
two actions for any of its still-open PRs: the real GitHub PR review IS the
approval step now, for every category equally -- not a subset of them via
an in-app gate row.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from agentit.audit import audit_log
from agentit.portal.helpers import get_current_user, get_store

log = logging.getLogger(__name__)

router = APIRouter()


def _redirect_target(assessment_id: str | None) -> str:
    """Same "land on the Actions tab, not the Overview tab" convention the
    retired ``routes/gates.py::resolve_gate()`` used -- the next actionable
    item in the same queue is immediately visible."""
    if assessment_id:
        return f"/assessments/{assessment_id}?tab=actions"
    return "/ledger"


@router.post("/prs/merge", response_model=None)
async def merge_pr_route(request: Request):
    form = await request.form()
    pr_url = str(form.get("pr_url") or "")
    assessment_id = str(form.get("assessment_id") or "") or None
    if not pr_url:
        raise HTTPException(400, "pr_url required")
    actor = get_current_user(request)
    s = await get_store()

    from agentit.portal.github_pr import merge_pr

    result = await asyncio.to_thread(merge_pr, pr_url)
    target = _redirect_target(assessment_id)
    sep = "&" if "?" in target else "?"

    if "error" in result:
        log.warning("PR merge failed for %s: %s", pr_url, result["error"])
        audit_log(actor=actor, action="pr-merge", resource=pr_url, outcome="error", details=result)
        return RedirectResponse(
            url=f"{target}{sep}error={quote('PR merge failed: ' + str(result['error'])[:150])}",
            status_code=303,
        )

    audit_log(actor=actor, action="pr-merge", resource=pr_url, outcome="merged")
    try:
        await s.log_event("human", "gitops-pr-merged", None, "info", f"Merged PR {pr_url}")
    except Exception:
        log.warning("Failed to log gitops-pr-merged event for %s", pr_url, exc_info=True)

    return RedirectResponse(
        url=f"{target}{sep}success={quote('Merged ' + pr_url)}",
        status_code=303,
    )


@router.post("/prs/close", response_model=None)
async def close_pr_route(request: Request):
    form = await request.form()
    pr_url = str(form.get("pr_url") or "")
    reason = str(form.get("reason") or "")
    assessment_id = str(form.get("assessment_id") or "") or None
    app_name = str(form.get("app_name") or "")
    category = str(form.get("category") or "")
    if not pr_url:
        raise HTTPException(400, "pr_url required")
    actor = get_current_user(request)
    s = await get_store()

    from agentit.portal.github_pr import close_pr

    result = await asyncio.to_thread(close_pr, pr_url, reason)
    target = _redirect_target(assessment_id)
    sep = "&" if "?" in target else "?"

    if "error" in result:
        log.warning("PR close failed for %s: %s", pr_url, result["error"])
        audit_log(actor=actor, action="pr-close", resource=pr_url, outcome="error", details=result)
        return RedirectResponse(
            url=f"{target}{sep}error={quote('PR close failed: ' + str(result['error'])[:150])}",
            status_code=303,
        )

    audit_log(actor=actor, action="pr-close", resource=pr_url, outcome="closed", details={"reason": reason})
    try:
        await s.log_event(
            "human", "gitops-pr-closed", app_name or None, "info",
            f"Closed PR {pr_url} without merging" + (f": {reason}" if reason else ""),
        )
    except Exception:
        log.warning("Failed to log gitops-pr-closed event for %s", pr_url, exc_info=True)

    # Record the outcome immediately, from the reason the human just typed
    # -- more reliable than waiting for the next page load's PR-outcomes
    # sync to re-derive it from GitHub comments (see pr_outcomes.py), and
    # it still posts the same reason as a real PR comment (github_pr.
    # close_pr) so a later poll finds the identical signal either way.
    if hasattr(s, "record_pr_outcome") and app_name:
        try:
            await s.record_pr_outcome(
                pr_url, app_name, "rejected",
                assessment_id=assessment_id, category=category, reject_reason=reason,
            )
        except Exception:
            log.warning("Failed to record pr_outcome for closed PR %s", pr_url, exc_info=True)

    return RedirectResponse(url=f"{target}{sep}success=PR+closed", status_code=303)
