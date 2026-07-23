"""Assessment lifecycle: create, view, onboard, apply, and PR creation.

The actual assess/onboard/deliver pipeline orchestration (background-thread
and ``BackgroundTasks`` job bridging, the clone+assess work, the automatic
validate-fix-deliver chain) lives in ``agentit.portal.services.
assess_pipeline``/``onboard_pipeline`` (2026-07-20 reuse/refactor review --
this file used to be ~2000 lines mixing that orchestration in with the HTTP
route handlers below). Every ``@router.post``/``@router.get`` function here
stays a thin wrapper: request parsing, a call into the pipeline/store, and
building the HTTP response.

Kept in this file rather than moved: the unified-progress-stepper position
helpers (``_assess_pipeline_position``/``_onboard_pipeline_position``/
``_onboard_terminal_redirect_url``/``_onboard_agent_steps`` and their
supporting constants) -- these compute what a route should render/redirect
to given a job's real status, which is response-building logic the routes
below need directly, not pipeline orchestration that mutates job/store
state.
"""
from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from agentit.models import Severity
from agentit.portal.cluster_apply import install_operator
from agentit.portal.helpers import get_current_user, get_store, get_templates
from agentit.portal.services.assess_pipeline import _auto_create_infra_repo, start_assess_job
from agentit.portal.services.onboard_pipeline import start_manual_validation_job, start_onboarding_job

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/assess")
async def assess_form():
    """Redirect to fleet with modal open — single entry point for assessment."""
    return RedirectResponse(url="/fleet?assess=1", status_code=303)


@router.post("/assess", response_model=None)
async def assess_submit(
    request: Request,
    repo_url: str = Form(...),
    criticality: str = Form("medium"),
    infra_repo_url: str = Form(""),
    continue_onboard: str = Form("1"),
):
    infra = infra_repo_url.strip() or None
    # Fail fast, synchronously, for the one failure mode that's cheap to
    # check without a network call and doesn't need the background job at
    # all: a human-supplied infra repo URL on a host
    # AGENTIT_TRUSTED_GIT_DOMAINS doesn't allow. Every other mandatory-
    # registration failure (auto-create permission errors, a trusted-domain
    # rejection of an *auto-created* repo) can only be known after the
    # background job actually attempts it -- see
    # `_resolve_mandatory_infra_repo_url()`/`InfraRepoRequiredError` in
    # `_assess_sync()` below, which is the real, always-enforced gate this
    # is just an early, friendlier front door for.
    if infra is not None:
        from agentit.portal.github_pr import is_trusted_git_host
        if not is_trusted_git_host(infra):
            return RedirectResponse(
                url=(
                    "/fleet?assess=1&error="
                    f"{quote(f'GitOps infra repo {infra!r} is not on a trusted Git host -- Assess cannot proceed without a usable GitOps infra repo.')}"
                ),
                status_code=303,
            )
    s = await get_store()
    # Chaining into onboarding is now the default for every Assess, not
    # just Fleet's "Scan" button (docs/onboarding-loop-vision-
    # gap-analysis.md §2/§8: the vision's "no difference between assessment
    # and onboarding" step, confirmed as a deliberate product decision, not
    # a free consolidation -- it removes the one point a human sees raw
    # findings before Onboard generates fixes for all of them). A caller
    # can still opt out by explicitly posting continue_onboard=0/false/"" --
    # nothing today does, but the mechanism (this Form field) stays
    # available rather than being removed outright.
    # Direct callers (tests/test_assess_submit_postgres.py) bypass FastAPI
    # Form injection, so the default may still be a Form() object -- treat
    # that the same as an explicit opt-out rather than silently defaulting
    # to True for a caller that never resolved the field at all.
    continue_flag = continue_onboard if isinstance(continue_onboard, str) else ""
    chain = continue_flag.strip().lower() in ("1", "true", "yes", "on")
    # The real clone+assess pipeline, its background-thread/event-loop
    # bridging, and (once complete) the deterministic chain into onboarding
    # all live in `services/assess_pipeline.py::start_assess_job()` -- see
    # that module's docstring for why the threading semantics are unchanged
    # by this call moving out of this route.
    job_id = await start_assess_job(request, s, repo_url, criticality, infra, chain)
    return RedirectResponse(url=f"/assess/progress/{job_id}", status_code=303)


# ── Unified Scan progress stepper ─────────────────────────────────────
# Three human stages on ONE shared roadmap: Assess → Generate → Open PR /
# waiting for merge. Rendered by _macros.html's `pipeline_stepper()` on
# BOTH assess_progress.html and _onboard_progress_fragment.html. Does NOT
# merge the two underlying jobs/routes/redirect (still two remediation_jobs
# rows + a real 303 hand-off). These helpers map each job's real status
# onto that 3-stage roadmap so both templates agree on position.
_PIPELINE_STAGE_COUNT = 3

# All assess-phase statuses light Assess (0). "completed" advances to
# Generate so a chained poll never looks stuck on Assess before the 303.
_ASSESS_STAGE_INDEX = {"cloning": 0, "assessing": 0, "saving": 0, "completed": 1}

_ONBOARD_STAGE_INDEX = {
    "pending": 1, "running": 1, "saving": 1,
    "validating": 2, "reviewing": 2, "delivering": 2, "needs_attention": 2,
    "completed": _PIPELINE_STAGE_COUNT,
}


def _assess_pipeline_position(job: dict) -> tuple[int, bool]:
    """Where this assess job sits on the unified 3-stage Scan roadmap, and
    whether it represents a failure. A failed assess job's
    ``current_step`` holds a free-form error message (see
    ``assess_submit()``'s ``except`` blocks), not a stage keyword -- flag
    failure on Assess and leave the explanation to the error alert.
    """
    if job["status"] == "failed":
        return 0, True
    return _ASSESS_STAGE_INDEX.get(job["status"], 0), False


def _onboard_pipeline_position(job: dict) -> tuple[int, bool]:
    """Where this onboard job sits on the same 3-stage Scan roadmap
    (Generate / Open PR) -- mirrors ``_assess_pipeline_position()``."""
    if job["status"] == "failed":
        return 1, True
    return _ONBOARD_STAGE_INDEX.get(job["status"], 1), False


@router.get("/assess/progress/{job_id}", response_class=HTMLResponse)
async def assess_progress(request: Request, job_id: str):
    """Passive progress viewer only (2026-07-20) -- the assess->onboard
    chain itself is now fired deterministically, server-side, by whichever
    background job actually performed the assessment
    (``assess_submit()``'s thread / ``webhook_assess()`` /
    ``webhook_github_push()``), immediately once the new assessment is
    saved. It no longer depends on a browser polling this route at all: a
    closed tab (root cause #1 of the earlier Onboard/Scan investigation,
    PR #99) can no longer silently skip the chain.

    This route's only remaining job is to redirect a human still watching
    to wherever the chain already is: onboarding's own progress page if an
    onboard job already exists for this assessment (``list_remediation_
    jobs()`` -- filtered to exclude this assess job's own row, since both
    kinds of job share the same ``remediation_jobs`` table), or plain
    Assessment Detail if this assess explicitly opted out of chaining
    (``continue_onboard=0``, still available -- see ``assess_submit()`` --
    even though no real caller sets it today). That redirect is a real
    HTTP 303, unchanged by the 2026-07-20 unified-stepper fix above: this
    page's own ``hx-get`` self-poll (see assess_progress.html) already
    follows it as an in-place AJAX swap, never a full browser navigation,
    so there was nothing unreliable to fix there -- only the differently-
    shaped stepper swapped in on the other end of it.
    """
    s = await get_store()
    job = await s.get_remediation_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job["status"] == "completed" and job.get("assessment_id"):
        assessment_id = job["assessment_id"]
        onboard_jobs = [
            j for j in await s.list_remediation_jobs(assessment_id) if j["id"] != job_id
        ]
        if onboard_jobs:
            onboard_job_id = onboard_jobs[0]["id"]
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard/progress/{onboard_job_id}",
                status_code=303,
            )
        return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)

    chained = "continue_onboard" in (job.get("steps_completed") or [])
    current_index, failed = _assess_pipeline_position(job)
    return get_templates().TemplateResponse(request, "assess_progress.html", {
        "job": job, "job_id": job_id,
        "current_index": current_index, "failed": failed,
        # Opt-out of chaining: only Assess will run for this job.
        "stage_count": _PIPELINE_STAGE_COUNT if chained else 1,
    })


@router.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def assessment_detail(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    scores_sorted = sorted(report.scores, key=lambda sc: sc.score)
    urgent_findings = [
        f
        for sc in report.scores
        for f in sc.findings
        if f.severity in (Severity.critical, Severity.high)
    ]

    slos = await s.list_slos(assessment_id)
    onboardings = await s.list_onboardings(assessment_id)

    from agentit.portal.check_catalog import badge_for_category
    from agentit.remediation.registry import allows_auto_pr, lookup
    # Computed from every finding (not just urgent_findings' critical/high
    # subset) so the same set correctly covers the Remediation Plan table
    # below, which lists findings of every severity -- one source of truth
    # for "is this finding/plan-item fixable", shared by both loops in the
    # template instead of each re-deriving visibility independently
    # (docs/ui-redesign-proposal.md §0's second bug).
    # Fix CTA only for remediable (auto_pr) contracts — detect_only never.
    all_findings = [f for sc in report.scores for f in sc.findings]
    fixable_categories = {
        f.category for f in all_findings
        if lookup(f.category) is not None and allows_auto_pr(f.category)
    }
    finding_badges = {
        f.category: badge_for_category(f.category) for f in all_findings if f.category
    }
    from agentit.scoring import letter_grade, top_fix_impacts
    top_fixes = top_fix_impacts(report, remediable_categories=fixable_categories, limit=3)

    # Actions tab: every real thing still waiting on a human for this app --
    # an open, unmerged PR (Merge/Close, routes/pr_actions.py -- the real
    # GitHub PR review IS the approval step now, no gate involved), an
    # unresolved rollback recommendation, or an unresolved escalated
    # finding (both plain events -- see routes/recommendations.py and
    # portal/pending_actions.py).
    from agentit.portal.delivery import (
        get_next_action_state,
        is_gitops_registered,
        is_self_managed_delivery_target,
    )
    from agentit.portal.escalation_guidance import enrich_escalations
    from agentit.portal.pending_actions import list_unresolved_recommendations
    from agentit.portal.pr_tracking import get_app_pr_history

    pr_history = await get_app_pr_history(s, assessment_id, report.repo_url, report.repo_name)
    open_prs = [pr for pr in pr_history if pr["state"] == "open"]
    from agentit.remediation.clear_evidence import contract_lines_for_portal

    from agentit.portal.pr_tracking import enrich_decision_card

    finding_decision_prs: dict[str, dict] = {}
    for pr in open_prs:
        pr["kind"] = "pr"
        # Honesty line on PR cards: Clears X by Y (solution contract).
        pr["contract_lines"] = contract_lines_for_portal(pr.get("target_findings") or [])
        targets = {str(x).lower() for x in (pr.get("target_findings") or [])}
        pr["_target_set"] = targets
        for fix in top_fixes:
            cat = (fix.get("category") or "").lower()
            if cat and cat in targets and fix.get("estimated_delta"):
                pr["estimated_delta"] = max(
                    pr.get("estimated_delta") or 0.0,
                    float(fix["estimated_delta"]),
                )
        enrich_decision_card(pr)
        if pr.get("lifecycle") == "needs_approval" or pr.get("state") == "open":
            for tgt in targets:
                finding_decision_prs.setdefault(tgt, pr)
    for fix in top_fixes:
        cat = (fix.get("category") or "").lower()
        fix["pr"] = next(
            (pr for pr in open_prs if cat and cat in pr.get("_target_set", set())),
            open_prs[0] if len(open_prs) == 1 else None,
        )
    merged_prs = [pr for pr in pr_history if pr.get("state") == "merged" or pr.get("lifecycle") == "merged"]

    unresolved_rollbacks, unresolved_escalations = await list_unresolved_recommendations(
        s, target_app=report.repo_name,
    )
    for e in unresolved_rollbacks:
        e["kind"] = "rollback"
    for e in unresolved_escalations:
        e["kind"] = "escalation"
    # Ledger escalations: real why/how guidance (never invent failure detail).
    await enrich_escalations(s, unresolved_escalations, report)

    pending_actions = open_prs + unresolved_rollbacks + unresolved_escalations

    # Every PR-shaped delivery (including the CI/CD-shared-namespace
    # variant) is already covered by the PR list above (``pr_history``,
    # lifecycle="needs_approval") -- rendering it again here would just
    # duplicate the same "Approve & Deliver" a second time on this page.
    # The two recommendation types (``rollback-review``,
    # ``finding-unresolved-escalation``) have no PR of their own to point
    # at -- a human acknowledgment with nothing to review on GitHub -- so
    # they get recommendation_card instead. There's no ``gate_type`` left
    # to filter on (see docs/unified-apply-flow.md), so `kind` is the real
    # (and only) distinction.
    non_pr_pending_actions = [g for g in pending_actions if g.get("kind") != "pr"]

    # Real "what happens next" fact (docs/onboarding-loop-vision-gap-
    # analysis.md's Step 8 discussion / Phase 5) -- reuses
    # `unresolved_escalations` (already fetched above, already scoped to
    # this app) instead of a second query.
    # Founder mental model: when open PRs exist, merge on GitHub is the
    # primary next action — not escalated findings / Ledger review.
    next_action = await get_next_action_state(
        s, report.repo_name, repo_url=report.repo_url, assessment_id=assessment_id,
        unresolved_escalations=unresolved_escalations,
    )
    if open_prs:
        n = len(open_prs)
        next_action = {
            "state": "open_prs",
            "label": "Open PRs waiting",
            "message": (
                f"{n} open PR{'s' if n != 1 else ''} waiting — review and merge on GitHub "
                f"(see Open PRs below)."
            ),
            "open_pr_count": n,
            "assessment_id": assessment_id,
        }

    # Real, specific empty-state copy for the Ledger tab's approval section
    # (docs/ux-design-requirements.md checklist #10) -- how many of THIS
    # app's actions (merged/closed PRs, rollback/escalation events resolved
    # the same way -- a real resolving event correlated to the original
    # one) were actually resolved recently, instead of a bare "nothing here".
    recently_resolved_actions_count = 0
    if not pending_actions:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recently_resolved_actions_count += sum(
            1 for pr in pr_history
            if pr["state"] in ("merged", "closed") and (pr.get("merged_at") or pr.get("created_at") or "") >= cutoff
        )
        recent_events = await s.list_events(target_app=report.repo_name, limit=200)
        recently_resolved_actions_count += sum(
            1 for e in recent_events
            if e["action"] in ("rollback-executed", "rollback-dismissed", "finding-escalation-acknowledged")
            and e["timestamp"] >= cutoff
        )

    gitops_registered, infra_repo_url = await is_gitops_registered(report.repo_name, report)

    # Surface a silent _auto_create_infra_repo() failure (assessments.py's
    # main assess_submit background job) as a real, visible banner here --
    # not just a server log line. Only flagged while it's still the *most
    # recent* fact about this app's infra-repo state: a later successful
    # "gitops-registered" event (e.g. from a manual Register-for-GitOps
    # retry) naturally clears it with no extra bookkeeping needed.
    infra_repo_creation_failed = False
    if not infra_repo_url and hasattr(s, "list_events"):
        recent_infra_events = await s.list_events(target_app=report.repo_name, limit=50)
        for ev in recent_infra_events:
            if ev["action"] in ("infra-repo-creation-failed", "gitops-registered"):
                infra_repo_creation_failed = ev["action"] == "infra-repo-creation-failed"
                break

    trend = await s.get_trend(report.repo_url) if hasattr(s, 'get_trend') else {}
    score_history = await s.get_score_history(report.repo_url) if hasattr(s, 'get_score_history') else []
    for i, h in enumerate(score_history):
        h["delta"] = round(h["overall_score"] - score_history[i - 1]["overall_score"], 2) if i > 0 else None
    apply_results = await s.get_apply_results(assessment_id)

    app_name = report.repo_name
    schedules_exist = await s.has_schedules_for_app(app_name) if hasattr(s, 'has_schedules_for_app') else False
    # PR-only HITL: "applied" means a human merged a delivery PR — never
    # invent Applied from empty apply_results (Direct Apply is gone).
    has_merged_delivery = bool(merged_prs) or bool(
        apply_results and apply_results.get("applied")
    )
    if has_merged_delivery and (slos or schedules_exist):
        lifecycle_stage = "monitored"
    elif has_merged_delivery:
        lifecycle_stage = "applied"
    elif onboardings or open_prs:
        lifecycle_stage = "onboarded"
    else:
        lifecycle_stage = "assessed"

    # True when an older assessment of this repo already onboarded — so a
    # fresh "assessed" stage after Re-assess is a refresh, not first-time.
    prior_onboarded = False
    # The single-button Scan flow (2026-07-20) always chains straight into
    # onboarding server-side the moment a new assessment is saved
    # (assess_submit()'s background thread / webhook_assess() /
    # webhook_github_push() -- see README's Unified apply flow entry) --
    # "assessed" with no error is now a genuinely transient state
    # (onboarding is either already running or about to be, not a button
    # waiting on a human). list_remediation_jobs() (ordered newest-first)
    # finds that already-running/failed job, if any, so the page can say so
    # honestly instead of showing a stale "Onboard This App" call to action
    # that duplicates what Scan already does automatically.
    active_onboard_job = None
    failed_onboard_job = None
    needs_attention_onboard_job = None
    onboard_jobs = await s.list_remediation_jobs(assessment_id) if hasattr(s, "list_remediation_jobs") else []
    if onboard_jobs:
        latest_onboard_job = onboard_jobs[0]
        if latest_onboard_job["status"] == "needs_attention":
            needs_attention_onboard_job = latest_onboard_job
        elif latest_onboard_job["status"] == "failed":
            failed_onboard_job = latest_onboard_job
        elif latest_onboard_job["status"] not in _ONBOARD_JOB_TERMINAL_STATUSES:
            active_onboard_job = latest_onboard_job
    if lifecycle_stage == "assessed" and hasattr(s, "repo_has_onboarding"):
        prior_onboarded = await s.repo_has_onboarding(report.repo_url)

    # Edge case only: needs_attention left saved manifests without a clean
    # PR — bury Onboard Results as a secondary link, never compete with Scan.
    show_onboard_results_link = needs_attention_onboard_job is not None and bool(onboardings)

    self_managed = await is_self_managed_delivery_target(report.repo_name, report)

    suppressions = await s.get_suppressions(report.repo_name)

    # A genuine, real milestone (docs/ux-design-requirements.md checklist
    # #9) -- the FIRST time this app reaches a perfect score, never on
    # every routine assessment that happens to already be at 100. Derived
    # entirely from this app's own real score history, never fabricated.
    celebrate_first_perfect_score = report.overall_score >= 100 and not any(
        h["overall_score"] >= 100 for h in score_history if h["id"] != assessment_id
    )

    # pr_history/open_prs (Open PRs section + PR History tab) are already
    # computed above, alongside pending_actions.

    assessment_cadence = await s.get_assessment_cadence(report.repo_url)
    # Real signal (same one schedules.py's "Long-Lived Agents" table and
    # Capabilities' Self-Improvement tab already use), not a chart-intent
    # default -- so the cadence dropdown's help text never claims automatic
    # re-assessment is happening when watchers/reassess_scheduler.py isn't
    # actually deployed/ticking anywhere in this cluster.
    from agentit.portal.routes.capabilities import watcher_heartbeat_status
    _reassess_hb = watcher_heartbeat_status(await s.list_agents(), "reassess-scheduler", 2 * 86400)
    reassess_scheduler_active = bool(_reassess_hb["has_run"]) and not _reassess_hb["stale"]

    return get_templates().TemplateResponse(
        request,
        "assessment_detail.html",
        {
            "report": report,
            "scores_sorted": scores_sorted,
            "urgent_findings": urgent_findings,
            "assessment_id": assessment_id,
            "slo_count": len(slos),
            "onboarding_count": len(onboardings),
            "fixable_categories": fixable_categories,
            "finding_badges": finding_badges,
            "pending_actions": pending_actions,
            "non_pr_pending_actions": non_pr_pending_actions,
            "next_action": next_action,
            "gitops_registered": gitops_registered,
            "infra_repo_url": infra_repo_url,
            "infra_repo_creation_failed": infra_repo_creation_failed,
            "trend": trend,
            "score_history": score_history,
            "lifecycle_stage": lifecycle_stage,
            "prior_onboarded": prior_onboarded,
            "active_onboard_job": active_onboard_job,
            "failed_onboard_job": failed_onboard_job,
            "needs_attention_onboard_job": needs_attention_onboard_job,
            "show_onboard_results_link": show_onboard_results_link,
            "self_managed": self_managed,
            "suppressions": suppressions,
            "celebrate_first_perfect_score": celebrate_first_perfect_score,
            "recently_resolved_actions_count": recently_resolved_actions_count,
            "open_prs": open_prs,
            "pr_history": pr_history,
            "finding_decision_prs": finding_decision_prs,
            "assessment_cadence": assessment_cadence,
            "reassess_scheduler_active": reassess_scheduler_active,
            "top_fixes": top_fixes,
            "score_letter": letter_grade(report.overall_score),
        },
    )


@router.post("/assessments/{assessment_id}/register-gitops", response_model=None)
async def register_gitops(request: Request, assessment_id: str):
    """Lightweight GitOps registration for an already-assessed app --
    the nudge action docs/ui-redesign-proposal.md §4 recommends for
    unregistered apps: register now (auto-creating an infra repo when none
    is supplied, mirroring `_auto_create_infra_repo`) rather than requiring
    a full re-assessment with the GitOps Infra Repo field filled in from
    scratch.
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    form = await request.form()
    infra_repo_url = str(form.get("infra_repo_url", "")).strip() or None

    if infra_repo_url is None:
        infra_repo_url = await asyncio.to_thread(_auto_create_infra_repo, report.repo_url)
        if infra_repo_url is None:
            return RedirectResponse(
                url=(
                    f"/assessments/{assessment_id}?error="
                    f"{quote('Could not auto-create a GitOps infra repo — paste an infra repo URL in the Register form (or check GITHUB_TOKEN permissions) and try again.')}"
                ),
                status_code=303,
            )

    await s.set_infra_repo_url(assessment_id, infra_repo_url)

    from agentit.portal.github_pr import ensure_applicationset
    ensured = await asyncio.to_thread(ensure_applicationset, infra_repo_url)
    if not ensured:
        return RedirectResponse(
            url=(
                f"/assessments/{assessment_id}?warning="
                f"{quote('Infra repo registered, but the Argo CD ApplicationSet could not be confirmed on the cluster — it will be ensured automatically on the next delivery.')}"
            ),
            status_code=303,
        )

    await s.log_event("portal", "gitops-registered", report.repo_name, "info",
                       f"Registered for GitOps delivery via {infra_repo_url}")
    # Not "Registered for GitOps" -- the ApplicationSet only *discovers* an
    # app once a delivery actually commits manifests under apps/{app}/ in the
    # infra repo and that PR is merged (delivery.is_gitops_registered() keys
    # off a live Application CR, not just this URL being set). Overstating
    # completion here is exactly what made the button look like it "did
    # nothing" -- the badge/nudge never changed to match the claim.
    return RedirectResponse(
        url=(
            f"/assessments/{assessment_id}?success="
            f"{quote('GitOps infra repo configured via ' + infra_repo_url + '. This app will show as GitOps-registered once your next Scan delivery PR is committed and merged there.')}"
        ),
        status_code=303,
    )


@router.post("/assessments/{assessment_id}/set-cadence", response_model=None)
async def set_assessment_cadence(request: Request, assessment_id: str):
    """Sets how often this app is automatically re-Assessed
    (``apps.assessment_cadence``: daily/weekly/monthly/manual) --
    read by ``watchers/reassess_scheduler.py``'s tick loop, which triggers
    the re-assessment through the same ``/api/webhook/assess`` route the
    manual Fleet Re-assess button already uses. Saving a cadence here is
    real (persisted on the app row) regardless of whether that watcher is
    currently deployed/enabled -- see ``assessment_detail.html``'s own
    honest copy for the "nothing is running to act on it yet" case.
    """
    from agentit.portal.store import ASSESSMENT_CADENCES

    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    form = await request.form()
    cadence = str(form.get("cadence", "")).strip()
    if cadence not in ASSESSMENT_CADENCES:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}?error={quote('Invalid re-assessment cadence.')}",
            status_code=303,
        )

    await s.set_assessment_cadence(report.repo_url, cadence)
    await s.log_event(
        "portal", "assessment-cadence-changed", report.repo_name, "info",
        f"Automatic re-assessment cadence set to: {cadence}",
    )
    return RedirectResponse(
        url=f"/assessments/{assessment_id}?success={quote('Re-assessment cadence updated.')}",
        status_code=303,
    )


@router.post("/assessments/{assessment_id}/fix")
async def fix_finding(request: Request, assessment_id: str):
    """Dispatch a single finding fix via the generic remediation dispatcher."""
    form = await request.form()
    category = str(form.get("category", ""))
    description = str(form.get("description", ""))

    if not category:
        raise HTTPException(400, "category required")

    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(404, "Assessment not found")

    # RemediationDispatcher is now genuinely async -- await it directly,
    # no more .raw/to_thread bridge needed for this call path.
    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(s)
    result = await dispatcher.dispatch(assessment_id, category, report.repo_name)

    from agentit.portal.metrics import remediations_total as _rt
    _status = "success" if result["files"] else ("error" if result.get("error") else "empty")
    _rt.labels(agent=result.get("agent", "unknown"), status=_status).inc()

    if result.get("error") and not result["files"]:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}?error={quote(result['error'])}",
            status_code=303,
        )

    if result["files"]:
        # Pure generation step -- no direct apply here. Matching what
        # "Onboard This App" already is for a whole plan, `fix_finding()`
        # only generates files; "Deliver" (on Onboard Results) is the one
        # and only verb that ever calls route_and_deliver()/
        # apply_with_verification() (docs/ui-redesign-proposal.md §0/§3).
        # A prior version ran a raw, unaudited
        # apply_manifests_to_cluster(dry_run=True) here -- dead work whose
        # result was immediately superseded by Deliver, and a bypass of the
        # unified delivery router for a GitOps-registered app. This no
        # longer persists a separate `remediations` row either -- the
        # "fix-generated" event below is the durable record, and the real
        # outcome is tracked by Deliver's own `deliveries`/`gates` rows once
        # a human actually delivers it.
        await s.log_event(
            "dispatcher", "fix-generated", report.repo_name, "info",
            f"Generated {len(result['files'])} fix(es) for '{category}' via {result['agent']}",
        )
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?fix_generated={len(result['files'])}&agent={result['agent']}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/assessments/{assessment_id}?error={quote('No fix generated for this finding')}",
        status_code=303,
    )


@router.get("/api/assessments")
async def api_list() -> JSONResponse:
    s = await get_store()
    return JSONResponse(await s.list_all())


@router.get("/api/assessments/{assessment_id}")
async def api_detail(assessment_id: str) -> JSONResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return JSONResponse(report.model_dump(mode="json"))


@router.post("/assessments/{assessment_id}/delete", response_model=None)
async def delete_assessment(assessment_id: str):
    s = await get_store()
    if not await s.delete(assessment_id):
        raise HTTPException(404, "Assessment not found")
    await s.log_event("portal", "assessment-deleted", None, "info", f"Deleted assessment {assessment_id}")
    return RedirectResponse(url="/", status_code=303)


@router.get("/assessments/{assessment_id}/onboarding-history", response_class=HTMLResponse)
async def onboarding_history(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    onboardings = await s.list_onboardings(assessment_id)
    pr_urls = [ob["pr_url"] for ob in onboardings if ob.get("pr_url")]
    if pr_urls:
        from agentit.portal.github_pr import get_pr_status
        statuses = await asyncio.gather(
            *(asyncio.to_thread(get_pr_status, url) for url in pr_urls)
        )
        status_map = dict(zip(pr_urls, statuses))
        for ob in onboardings:
            if ob.get("pr_url"):
                ob["pr_status"] = status_map.get(ob["pr_url"], {})
    return get_templates().TemplateResponse(request, "onboarding_history.html", {
        "report": report,
        "onboardings": onboardings,
        "assessment_id": assessment_id,
    })


@router.post("/assessments/{assessment_id}/onboard", response_model=None)
async def onboard_submit(
    request: Request,
    assessment_id: str,
    background_tasks: BackgroundTasks,
):
    """Kicks off onboarding as a background job and immediately redirects to
    a real-time progress page (docs/ux-design-requirements.md checklist #6)
    instead of blocking the request for however long agent orchestration
    takes -- mirrors ``assess_submit()``'s existing job-tracking pattern.

    ``start_onboarding_job()`` (``services/onboard_pipeline.py``) owns the
    actual job-creation/``BackgroundTasks`` scheduling -- this route stays a
    thin wrapper: look up the assessment, delegate, build the redirect.
    Once manifests are generated, ``_run_onboarding_job()`` automatically
    runs the validate/fix loop, a final LLM review, and (once clean) the
    real Deliver -- see ``auto_delivery.py``. A human's one remaining
    action is reviewing/merging the resulting PR on GitHub; there is no
    more manual Dry Run/Deliver click for the common case (Onboard Results
    still exposes both by hand for the ``needs_attention`` fallback case).
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    job_id = await start_onboarding_job(s, assessment_id, request, background_tasks)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard/progress/{job_id}", status_code=303,
    )


# Job statuses that end a human's wait on the progress page -- no further
# polling/SSE ticks matter once one of these is reached.
# ``"needs_attention"`` is the honest fallback outcome (added alongside
# auto_delivery.py): manifests exist and were saved, but the automatic
# validate/fix loop couldn't converge, or the real delivery produced no
# PR -- distinct from ``"failed"`` (generation itself failed, no manifests
# exist at all).
_ONBOARD_JOB_TERMINAL_STATUSES = ("completed", "failed", "needs_attention")


async def _onboard_terminal_redirect_url(assessment_id: str, job: dict) -> str:
    """Where a human (or an SSE-driven client-side redirect) lands once an
    onboarding job reaches a terminal state. Shared by the direct GET
    redirect (``onboard_progress()``) and the SSE stream's server-rendered
    fragment (``onboard_progress_stream()`` -> ``_onboard_progress_fragment.
    html``'s inline ``<script>``) so the two can never disagree about the
    URL for the same job.

    - ``"failed"`` (onboarding generation itself failed, no manifests
      exist) -> Assessment Detail, with the error flash CLAUDE.md's
      "errors must always be visible" convention requires.
    - ``"needs_attention"`` (manifests exist, but the automatic validate/
      fix/deliver pipeline could not finish on its own) -> Onboard Results,
      with a warning flash naming what still needs a human's attention.
    - ``"completed"`` (the automatic pipeline actually opened one or more
      pull requests) -> Onboard Results, with a success flash -- the PR
      cards there already show the real, freshly-opened PR(s).
    """
    if job["status"] == "failed":
        return f"/assessments/{assessment_id}?error={quote(job.get('error') or 'Onboarding failed')}"
    if job["status"] == "needs_attention":
        return (
            f"/assessments/{assessment_id}/onboard-results?warning="
            f"{quote(job.get('error') or 'Automatic validation needs your attention')}"
        )
    return f"/assessments/{assessment_id}/onboard-results?auto_delivered=true"


@router.get("/assessments/{assessment_id}/onboard/progress/{job_id}", response_class=HTMLResponse)
async def onboard_progress(request: Request, assessment_id: str, job_id: str):
    """Real-time onboarding progress page -- htmx SSE-driven (checklist #8),
    with a per-agent step list sourced from the real events
    ``FleetOrchestrator`` already logs live (``list_events_by_correlation_id``),
    never a fabricated percentage."""
    s = await get_store()
    job = await s.get_remediation_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in _ONBOARD_JOB_TERMINAL_STATUSES:
        return RedirectResponse(
            url=await _onboard_terminal_redirect_url(assessment_id, job), status_code=303,
        )
    agent_steps = await _onboard_agent_steps(s, assessment_id)
    current_index, failed = _onboard_pipeline_position(job)
    return get_templates().TemplateResponse(request, "onboard_progress.html", {
        "job": job, "job_id": job_id, "assessment_id": assessment_id, "agent_steps": agent_steps,
        "current_index": current_index, "failed": failed,
    })


async def _onboard_agent_steps(store: object, assessment_id: str) -> list[dict]:
    """Real per-agent onboarding steps for this assessment, derived from the
    events ``FleetOrchestrator._log_event()`` already writes live (agent
    name + completed/failed/job-created/timeout action) -- reused as-is,
    never a second, parallel notification mechanism."""
    events = await store.list_events_by_correlation_id(assessment_id)
    steps = []
    seen = set()
    for e in events:
        agent = e.get("agent_id", "")
        action = e.get("action", "")
        if agent in ("portal", "image-builder", "onboarding") or not agent:
            continue
        key = (agent, action)
        if key in seen:
            continue
        seen.add(key)
        steps.append({"agent": agent, "action": action, "summary": e.get("summary", "")})
    return steps


@router.get("/assessments/{assessment_id}/onboard/progress/{job_id}/stream")
async def onboard_progress_stream(assessment_id: str, job_id: str):
    """htmx's documented SSE extension (hx-ext="sse") as the transport for
    real step-level onboarding progress (checklist #8) -- reuses the same
    events table + remediation_jobs tracking every other progress signal in
    this app already relies on, rather than a parallel notification
    mechanism. Polls server-side every second (cheap local store reads) and
    pushes an HTML fragment per tick; closes the stream once the job reaches
    a terminal state so the client's EventSource naturally disconnects.
    """
    async def _events():
        s = await get_store()
        templates = get_templates()
        deadline = asyncio.get_event_loop().time() + 600  # 10 min safety cap
        while True:
            job = await s.get_remediation_job(job_id)
            if job is None:
                break
            agent_steps = await _onboard_agent_steps(s, assessment_id)
            is_terminal = job["status"] in _ONBOARD_JOB_TERMINAL_STATUSES
            redirect_url = await _onboard_terminal_redirect_url(assessment_id, job) if is_terminal else None
            current_index, failed = _onboard_pipeline_position(job)
            html = templates.get_template("_onboard_progress_fragment.html").render(
                job=job, agent_steps=agent_steps, assessment_id=assessment_id, redirect_url=redirect_url,
                current_index=current_index, failed=failed,
            )
            # SSE framing: every line of the payload must be its own "data:"
            # line per the spec -- a bare multi-line payload silently
            # truncates to its first line on the client.
            payload = "\n".join(f"data: {line}" for line in html.splitlines())
            yield f"event: progress\n{payload}\n\n"
            if is_terminal or asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(1)

    return StreamingResponse(_events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@router.post("/assessments/{assessment_id}/onboard-results/edit-file", response_model=None)
async def edit_onboarding_file(request: Request, assessment_id: str):
    """The edit-before-apply flow: let a human edit the raw content of one
    generated file before it's delivered. Saves directly into the same
    ``onboarding_results`` row ``get_onboarding()``/``route_and_deliver()``
    read -- once this returns, the SAVED (possibly-edited) content is what
    a subsequent "Deliver" click actually delivers, closing the exact gap
    README's "Known gap" callout named (see docs/self-improvement-for-
    agentit.md and docs/unified-apply-flow.md, both of which cite it).

    YAML/YAML-adjacent edits are re-validated via ``validate_manifest()``
    (``agents/base.py`` -- the same structural check every generated
    manifest already goes through) before being persisted: a human's raw
    edit could introduce a syntax error or a structurally invalid manifest
    the original LLM/template output wouldn't have had, and this is the
    existing, reusable validation path rather than a new one built from
    scratch. An invalid edit is rejected outright -- never partially saved.

    Also clears any persisted ``apply_results`` for this assessment: those
    rows record whether a Dry Run (or a real delivery) passed against the
    content as it existed BEFORE this edit, and ``onboard_results.html``
    gates Apply/Commit on that same persisted state. Leaving it in place
    would keep Apply/Commit unlocked on the strength of a dry run that was
    never actually run against the content a human is about to deliver --
    a human must re-run Dry Run against the real, current content before
    Apply/Commit can unlock again.
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    form = await request.form()
    category = str(form.get("category", ""))
    path = str(form.get("path", ""))
    content = str(form.get("content", ""))
    if not category or not path:
        raise HTTPException(status_code=400, detail="category and path required")

    if path.endswith((".yaml", ".yml")):
        from agentit.agents.base import validate_manifest
        errors = validate_manifest(content)
        if errors:
            return RedirectResponse(
                url=(
                    f"/assessments/{assessment_id}/onboard-results?error="
                    f"{quote('Edit rejected — invalid manifest: ' + '; '.join(errors)[:300])}"
                ),
                status_code=303,
            )

    updated = await s.update_onboarding_file(assessment_id, category, path, content)
    if updated is None:
        raise HTTPException(status_code=404, detail="Onboarding file not found")

    await s.clear_apply_results(assessment_id)

    await s.log_event(
        "portal", "onboarding-file-edited", report.repo_name, "info",
        f"Edited generated file before delivery: {path} — any prior validation/delivery result is "
        "now stale; validation must be re-run against the edited content before Commit/Per-Agent PRs unlocks.",
        correlation_id=assessment_id,
    )
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?edited={quote(path)}",
        status_code=303,
    )


@router.post("/assessments/{assessment_id}/onboard-results/run-validation", response_model=None)
async def run_validation(assessment_id: str, background_tasks: BackgroundTasks):
    """Replaces the old manual "Dry Run" button: kicks off the same
    validate -> fix -> re-validate -> final review -> real Deliver pipeline
    onboarding already runs automatically, as a background job, redirecting
    to the exact same real-time progress page onboarding itself uses. A
    plain, un-auto-fixed dry run is no longer a separate action -- it is
    strictly the first, always-run step of this pipeline, so nothing a bare
    "Dry Run" click used to do is lost, only upgraded.

    ``start_manual_validation_job()`` (``services/onboard_pipeline.py``)
    owns the actual job-creation/``BackgroundTasks`` scheduling.
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if await s.get_onboarding(assessment_id) is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    job_id = await start_manual_validation_job(s, assessment_id, background_tasks)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard/progress/{job_id}", status_code=303,
    )


@router.get("/assessments/{assessment_id}/onboard-results", response_class=HTMLResponse)
async def onboard_results(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    # Edit-before-apply diff capture: any file a human has edited carries
    # both `content` (the saved, possibly-edited version) and
    # `original_content` (what was first generated) -- render a real
    # line-level diff for it here rather than inventing a second diff
    # format (see content_diff.py's module docstring for why no existing
    # diff-rendering convention applied).
    from agentit.portal.content_diff import diff_lines
    for f in files:
        if f.get("edited"):
            f["diff"] = diff_lines(f.get("original_content", ""), f["content"])

    grouped: dict[str, list[dict]] = {}
    for f in files:
        grouped.setdefault(f["category"], []).append(f)

    orchestration = await s.get_orchestration(assessment_id) or {}
    apply_results = await s.get_apply_results(assessment_id)

    missing_operators = {}
    if apply_results:
        from agentit.portal.cluster_apply import _CRD_TO_OPERATOR
        for skip_reason in apply_results.get("skipped", []):
            if "CRD not installed" in skip_reason:
                for kind, op in _CRD_TO_OPERATOR.items():
                    if kind in skip_reason:
                        missing_operators[kind] = op
        for err in apply_results.get("errors", []):
            if "resource mapping not found" in err.lower():
                for kind, op in _CRD_TO_OPERATOR.items():
                    if kind.lower() in err.lower():
                        missing_operators[kind] = op

    pr_status = None
    onboardings = await s.list_onboardings(assessment_id)
    pr_url = onboardings[0]["pr_url"] if onboardings and onboardings[0]["pr_url"] else ""
    if pr_url:
        from agentit.portal.github_pr import get_pr_status
        pr_status = await asyncio.to_thread(get_pr_status, pr_url)

    # Portal visibility for the unified apply flow (docs/unified-apply-
    # flow.md): compute, once per page load, which delivery mechanism a
    # real "Deliver" click will use and why -- shown both as a dry-run-style
    # preview here AND, verbatim (via `delivery_confirmation`), inside the
    # un-skippable confirm dialog at the actual point of delivery, so the
    # two can never say different things about the same decision.
    from agentit.portal.delivery import (
        CATEGORY_CICD_SHARED_NAMESPACE,
        CATEGORY_CLUSTER_CONFIG,
        CATEGORY_MANIFEST_AT_REST,
        CATEGORY_SOURCE_PATCH,
        confirmation_text,
        is_gitops_registered,
        is_self_managed_delivery_target,
        preview_delivery_groups,
        resolve_cluster_config_mechanism,
    )
    gitops_registered, infra_repo_url = await is_gitops_registered(report.repo_name, report)
    self_managed = await is_self_managed_delivery_target(report.repo_name, report)
    delivery_mechanism = resolve_cluster_config_mechanism(
        infra_repo_url, self_managed=self_managed, category=CATEGORY_CLUSTER_CONFIG,
    )
    delivery_confirmation = confirmation_text(
        delivery_mechanism,
        infra_repo_url=infra_repo_url,
        self_managed=self_managed,
        app_repo_url=report.repo_url,
    )
    deliveries = await s.list_deliveries(assessment_id) if hasattr(s, "list_deliveries") else []

    # ── PR-centric framing (replaces raw manifest/category plumbing) ──────
    # What this onboarding will open (or has already opened) as real pull
    # requests, grouped by the exact taxonomy/mechanism route_and_deliver()
    # itself uses -- never a second, drifting approximation of it (see
    # preview_delivery_groups()'s own docstring) -- merged with every real,
    # already-known PR for this assessment (pr_tracking.py; single source
    # of truth, same primitives Ledger/Assessment Detail's PR displays use).
    preview_groups = preview_delivery_groups(
        files,
        infra_repo_url=infra_repo_url,
        self_managed=self_managed,
        app_repo_url=report.repo_url,
    )

    from agentit.portal.pr_tracking import (
        annotate_lifecycle,
        collect_pr_records,
        resolve_pr_states,
        sync_and_attach_pr_outcomes,
    )
    pr_records = collect_pr_records(deliveries, onboardings)
    await resolve_pr_states(pr_records)
    pr_records = await sync_and_attach_pr_outcomes(s, pr_records)
    for r in pr_records:
        annotate_lifecycle(r)
    records_by_category: dict[str, list[dict]] = {}
    for r in pr_records:
        records_by_category.setdefault(r["category"], []).append(r)

    _TAXONOMY_ORDER = (
        CATEGORY_CLUSTER_CONFIG, CATEGORY_CICD_SHARED_NAMESPACE,
        CATEGORY_SOURCE_PATCH, CATEGORY_MANIFEST_AT_REST,
    )
    pr_cards = []
    for category in _TAXONOMY_ORDER:
        preview = preview_groups.get(category)
        # Records are already sorted newest-first (collect_pr_records()) --
        # the most recent real PR for this category is what's "current";
        # any older ones (a category re-delivered across separate Deliver
        # clicks) still surface, just not as the headline record.
        category_records = records_by_category.get(category, [])
        if not preview and not category_records:
            continue
        current, older = (category_records[0], category_records[1:]) if category_records else (None, [])
        pr_cards.append({
            "category": category,
            "repo_kind": (current or {}).get("repo_kind") or (preview or {}).get("repo_kind", ""),
            "mechanism_text": (preview or {}).get("confirmation", ""),
            "files": (preview or {}).get("files", []),
            "record": current,
            "older_records": older,
        })

    # Historical "onboarding" category rows (legacy Per-Agent PRs) are no
    # longer a product surface — Scan/auto_delivery owns PR creation.
    pr_opened_count = sum(1 for c in pr_cards if c["record"])
    pr_pending_count = len(pr_cards) - pr_opened_count

    # dry_run_done is still computed for API/tests that inspect apply_results
    # flash state; Scan (auto_delivery) is the only PR-creating path.
    apply_dry_ok = bool(
        apply_results
        and apply_results.get("dry_run")
        and not apply_results.get("errors")
    )
    delivered_ok = bool(
        apply_results
        and not apply_results.get("dry_run")
        and not apply_results.get("errors")
    )
    dry_run_flash = bool(request.query_params.get("dry_run_summary")) and not request.query_params.get("error")
    dry_run_done = delivered_ok or apply_dry_ok or dry_run_flash

    # Retry Scan delivery: same auto_validate_and_deliver() pipeline Scan
    # already runs — only when the latest onboard job needs attention and
    # nothing is open yet (not a competing "Commit" product).
    needs_attention_onboard = False
    if hasattr(s, "list_remediation_jobs"):
        onboard_jobs = await s.list_remediation_jobs(assessment_id)
        if onboard_jobs and onboard_jobs[0].get("status") == "needs_attention":
            needs_attention_onboard = True
    show_retry_scan_delivery = needs_attention_onboard and pr_opened_count == 0

    return get_templates().TemplateResponse(
        request,
        "onboard_results.html",
        {
            "report": report,
            "grouped": grouped,
            "assessment_id": assessment_id,
            "orchestration": orchestration,
            "apply_results": apply_results,
            "missing_operators": missing_operators,
            "pr_status": pr_status,
            "gitops_registered": gitops_registered,
            "delivery_mechanism": delivery_mechanism,
            "delivery_confirmation": delivery_confirmation,
            "deliveries": deliveries,
            "dry_run_done": dry_run_done,
            "pr_cards": pr_cards,
            "pr_opened_count": pr_opened_count,
            "pr_pending_count": pr_pending_count,
            "show_retry_scan_delivery": show_retry_scan_delivery,
        },
    )


@router.get("/api/assessments/{assessment_id}/manifests")
async def api_manifests(assessment_id: str) -> JSONResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    return JSONResponse(files)


@router.get("/api/assessments/{assessment_id}/manifests/download")
async def download_manifests(assessment_id: str):
    """Download all onboarding manifests as a zip file."""
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = f"{f['category']}/{f['path']}"
            zf.writestr(arcname, f["content"])
    buf.seek(0)

    filename = f"{report.repo_name}-onboarding.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/assessments/{assessment_id}/deliver", response_model=None)
async def deliver(request: Request, assessment_id: str):
    """The unified apply flow's single entry point (docs/unified-apply-
    flow.md section (A)): replaces the independent "Apply to Cluster" /
    "Create PR" buttons for cluster/app config with one decision, computed
    once via ``route_and_deliver()`` -- the mechanism (direct apply vs.
    GitOps commit+PR) is no longer a human choice, only ``dry_run`` is.
    """
    from agentit.assessment_diff import current_finding_keys
    from agentit.portal.delivery import DeliveryInProgressError, repo_kind_for_mechanism, route_and_deliver
    from agentit.portal.quality_prs import finding_gate_allows_pr, finding_gate_refuse_reason

    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    form = await request.form()
    dry_run = form.get("dry_run") == "true"
    namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")

    # Onboarding generates fixes for every finding in this assessment (not
    # one specific finding) -- target every one of them so a later
    # re-assessment's diff can correlate this whole-app delivery back to
    # the findings it was meant to clear (docs/onboarding-loop-vision-gap-
    # analysis.md Phase 3).
    target_findings = sorted(current_finding_keys(report))

    # Phase A: same finding gate as auto_validate_and_deliver — refuse
    # catalog-dump PRs when there are no remediable open findings / score
    # delta. Dry-run previews still run (validation only; no PR open).
    if not dry_run and not finding_gate_allows_pr(target_findings):
        reason = finding_gate_refuse_reason(target_findings)
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(reason)}",
            status_code=303,
        )

    # Manual Deliver used to call route_and_deliver with the full onboard
    # catalog whenever *any* remediable finding existed (Hello-World #31/#32,
    # pinky #12). Mirror auto_validate_and_deliver's remaining quality bar:
    # filter to open findings, strip wrong-layer companions, refuse oversized
    # catalog dumps, and require clear-evidence simulation.
    if not dry_run:
        from agentit.portal.quality_prs import (
            MAX_FILES_PER_CLUSTER_PR,
            clear_evidence_simulation_ok,
            filter_files_to_open_findings,
            strip_wrong_layer_companions,
        )
        from agentit.remediation.registry import remediable_findings

        repo_url = (getattr(report, "repo_url", None) or "").lower()
        if "octocat/hello-world" in repo_url:
            reason = (
                "Refusing Deliver for probe repo octocat/Hello-World — "
                "not a dogfood app; remove it from Fleet instead of opening GitOps PRs."
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?error={quote(reason)}",
                status_code=303,
            )

        try:
            remediable = remediable_findings(target_findings)
        except Exception:
            remediable = list(target_findings)
        gate_findings = remediable if remediable else list(target_findings)
        kept_files, drop_reasons = filter_files_to_open_findings(files, gate_findings)
        kept_files, layer_drops = strip_wrong_layer_companions(kept_files, gate_findings)
        if layer_drops:
            drop_reasons = list(drop_reasons or []) + layer_drops
        if not kept_files:
            reason = (
                "No generated files map to open findings — refusing PR. "
                + ("; ".join(drop_reasons[:5]) if drop_reasons else "empty after finding filter")
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?error={quote(reason)}",
                status_code=303,
            )
        if len(kept_files) > MAX_FILES_PER_CLUSTER_PR:
            reason = (
                f"Manual Deliver refused catalog dump ({len(kept_files)} files > "
                f"{MAX_FILES_PER_CLUSTER_PR} per-cluster cap). Use Scan auto-delivery "
                "which partitions by finding cluster, or deliver a smaller edited set."
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?error={quote(reason)}",
                status_code=303,
            )
        sim_ok, sim_reason = clear_evidence_simulation_ok(kept_files, gate_findings)
        if not sim_ok:
            reason = (
                "Clear-evidence simulation failed — refusing PR "
                f"(MERGE would not clear the finding): {sim_reason}"
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?error={quote(reason)}",
                status_code=303,
            )
        files = kept_files
        target_findings = gate_findings

    try:
        delivery = await route_and_deliver(
            files, app_name=report.repo_name, namespace=namespace,
            report=report, store=s, assessment_id=assessment_id,
            actor=get_current_user(request), dry_run=dry_run,
            target_findings=target_findings,
        )
    except DeliveryInProgressError as exc:
        # A concurrent delivery for this same app (another human, or the
        # automatic background validate-and-deliver pipeline) is already
        # mid-commit -- a clear, specific message, not the generic
        # "delivery failed" text below, since nothing here actually failed.
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(str(exc))}",
            status_code=303,
        )
    except Exception as exc:
        log.exception("Delivery failed for assessment %s", assessment_id)
        detail = getattr(exc, "detail", None) or str(exc)
        return RedirectResponse(
            url=(
                f"/assessments/{assessment_id}/onboard-results?error="
                f"{quote(f'Delivery failed: {str(detail)[:180]}. Nothing was applied — fix the issue above, then retry Deliver.')}"
            ),
            status_code=303,
        )

    outcomes = delivery["outcomes"]
    cluster_outcome = outcomes.get("cluster_config")
    params = [f"delivery_id={delivery['delivery_id']}", f"dry_run={'true' if dry_run else 'false'}"]
    if isinstance(cluster_outcome, dict) and "applied" in cluster_outcome:
        params.append(f"applied={len(cluster_outcome['applied'])}")
        params.append(f"skipped={len(cluster_outcome.get('skipped', []))}")
        params.append(f"errors={len(cluster_outcome.get('errors', []))}")

    # Every commit/PR-based outcome (cluster_config-via-GitOps, source_patch,
    # manifest_at_rest) shares deliver_with_verification()'s
    # {"pr_url"|"error", "dry_run", ...} shape -- surface every one of them,
    # not just cluster_config's, and not just the success case. Previously,
    # only a cluster_config "pr_url" was ever rendered here: a failed
    # commit_to_infra_repo()/create_source_patch_pr()/create_onboarding_pr()
    # (which returns {"error": ...} rather than raising) or a plain dry-run
    # preview produced a redirect with *no* params reflecting it at all --
    # the page reloaded looking identical to before the click, i.e. exactly
    # "commit and open PR doesn't do anything".
    pr_urls = [o["pr_url"] for o in outcomes.values() if isinstance(o, dict) and o.get("pr_url")]
    errors = [o["error"] for o in outcomes.values() if isinstance(o, dict) and o.get("error")]
    dry_run_previews = [
        f"{o.get('mechanism', cat)} ({len(o['files'])} file(s))"
        for cat, o in outcomes.items()
        if isinstance(o, dict) and o.get("dry_run") and "files" in o
    ]
    if pr_urls:
        params.append(f"pr_url={quote(pr_urls[0])}")
        # Which of the app's two repos (code vs. GitOps) pr_urls[0] actually
        # opened against -- traced from the real mechanism that produced it
        # (delivery["mechanisms"]), not guessed, so onboard_results.html's
        # flash alert can label the link instead of showing a bare PR URL.
        for cat, o in outcomes.items():
            if isinstance(o, dict) and o.get("pr_url") == pr_urls[0]:
                repo_kind = repo_kind_for_mechanism(delivery["mechanisms"].get(cat, ""))
                if repo_kind:
                    params.append(f"pr_url_repo={repo_kind}")
                break
    if errors:
        params.append(f"error={quote(' | '.join(errors)[:300])}")
    if dry_run_previews:
        params.append(f"dry_run_summary={quote(' + '.join(dry_run_previews))}")
    if outcomes.get("cicd_shared_namespace"):
        params.append("cicd_gate=true")

    # GitOps / PR dry-runs never hit save_apply_results() (direct-apply only).
    # Persist a dry-run row so onboard_results step rail dry_run_done stays
    # true after redirect / hx-boost — not only while dry_run_summary is in
    # the URL flash.
    already_persisted = isinstance(cluster_outcome, dict) and "applied" in cluster_outcome
    if dry_run and dry_run_previews and not errors and not already_persisted:
        preview_files = [
            {"path": path, "purpose": f"dry-run via {o.get('mechanism', cat)}"}
            for cat, o in outcomes.items()
            if isinstance(o, dict) and o.get("dry_run") and "files" in o
            for path in o["files"]
        ]
        await s.save_apply_results(
            assessment_id,
            {
                "applied": [],
                "skipped": [],
                "errors": [],
                "repo_files": preview_files,
            },
            namespace,
            dry_run=True,
        )

    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?{'&'.join(params)}",
        status_code=303,
    )


@router.post("/api/install-operator", response_model=None)
async def install_operator_endpoint(request: Request):
    """Install an OLM operator. Called from the missing prerequisites UI."""
    form = await request.form()
    package = str(form.get("package", ""))
    channel = str(form.get("channel", "stable"))
    source = str(form.get("source", "redhat-operators"))
    assessment_id = str(form.get("assessment_id", ""))

    if not package:
        raise HTTPException(400, "package required")

    result = await asyncio.to_thread(install_operator, package, channel, source)

    if assessment_id:
        error_param = f"&install_error={quote(result['error'][:400])}" if result.get("error") else ""
        return RedirectResponse(
            url=(
                f"/assessments/{assessment_id}/onboard-results"
                f"?operator_installed={package}&install_status={result['status']}{error_param}"
            ),
            status_code=303,
        )
    return JSONResponse(result)


@router.post("/api/feedback")
async def record_feedback_endpoint(request: Request):
    """Record human feedback on agent recommendations."""
    form = await request.form()
    s = await get_store()
    fid = await s.record_feedback(
        app_name=str(form.get("app_name", "")),
        agent_name=str(form.get("agent_name", "")),
        finding_category=str(form.get("finding_category", "")),
        action=str(form.get("action", "")),
        human_reason=str(form.get("reason", "")),
        original_value=str(form.get("original_value", "")),
        human_value=str(form.get("human_value", "")),
    )
    return {"status": "recorded", "feedback_id": fid}


@router.post("/api/suppress")
async def suppress_check_endpoint(request: Request):
    """Suppress a check for a specific app — it won't fire on future assessments.

    A genuinely reversible, low-stakes action (a later "Unsuppress" click
    undoes it; nothing cluster-side is touched) -- the Findings tab's
    Suppress button (assessment_detail.html) now calls this via htmx and
    optimistically hides the finding the instant it's submitted, reconciling
    only if this actually fails (docs/ux-design-requirements.md checklist
    #7). An htmx-originated call therefore returns a small JSON ack instead
    of the old full-page redirect -- no swap needed since the client
    already reflects the outcome; any other (non-htmx) caller keeps the
    original redirect-back-to-assessment behavior.
    """
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    check_source = str(form.get("check_source", ""))
    reason = str(form.get("reason", ""))
    assessment_id = str(form.get("assessment_id", ""))
    if not app_name or not check_source:
        raise HTTPException(status_code=400, detail="app_name and check_source required")
    s = await get_store()
    await s.suppress_check(app_name, check_source, reason)
    if request.headers.get("HX-Request") == "true":
        return JSONResponse({"status": "suppressed", "app_name": app_name, "check_source": check_source})
    if assessment_id:
        return RedirectResponse(f"/assessments/{assessment_id}", status_code=303)
    return {"status": "suppressed", "app_name": app_name, "check_source": check_source}


@router.post("/api/unsuppress")
async def unsuppress_check_endpoint(request: Request):
    """Remove a suppression for a check on a specific app."""
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    check_source = str(form.get("check_source", ""))
    assessment_id = str(form.get("assessment_id", ""))
    if not app_name or not check_source:
        raise HTTPException(status_code=400, detail="app_name and check_source required")
    s = await get_store()
    await s.unsuppress_check(app_name, check_source)
    if assessment_id:
        return RedirectResponse(f"/assessments/{assessment_id}", status_code=303)
    return {"status": "unsuppressed", "app_name": app_name, "check_source": check_source}


@router.get("/api/assessments/{assessment_id}/verify")
async def verify_properties(assessment_id: str):
    """Verify enterprise properties hold against this assessment's generated manifests.

    NOTE: this is a standalone API endpoint, not (yet) wired into the
    automatic onboarding/apply path -- verification only runs when this
    endpoint is called explicitly.
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if not report:
        raise HTTPException(404, "Assessment not found")
    files_data = await s.get_onboarding(assessment_id)
    if files_data is None:
        raise HTTPException(404, "Onboarding not found")

    from agentit.agents.base import GeneratedFile
    from agentit.property_verifier import verify_all_properties
    files = [
        GeneratedFile(
            path=f["path"],
            content=f["content"],
            description=f.get("description", f["path"]),
        )
        for f in files_data
    ]
    results = verify_all_properties(files)
    return {
        "app": report.repo_name,
        "results": [{"property": r.property_name, "passed": r.passed,
                     "checks": r.checks, "summary": r.summary()} for r in results],
        "all_passed": all(r.passed for r in results),
    }


@router.get("/api/assessments/{assessment_id}/resource-recommendations")
async def resource_recommendations(assessment_id: str):
    """Get resource tuning recommendations based on Prometheus data."""
    from agentit.resource_tuner import analyze_resource_usage

    s = await get_store()
    report = await s.get(assessment_id)
    if not report:
        raise HTTPException(404, "Assessment not found")
    recs = analyze_resource_usage(report.repo_name, report.repo_name)
    return {
        "recommendations": [
            {
                "type": r.resource_type,
                "current": r.current_value,
                "recommended": r.recommended_value,
                "reason": r.reason,
                "confidence": r.confidence,
            }
            for r in recs
        ]
    }


@router.get("/api/assessments/{assessment_id}/dependencies")
async def dependency_status(assessment_id: str):
    """Get dependency update status from GitHub PRs."""
    from agentit.dependency_manager import process_dependency_prs

    s = await get_store()
    report = await s.get(assessment_id)
    if not report:
        raise HTTPException(404, "Assessment not found")
    updates = process_dependency_prs(report.repo_url)
    return {
        "updates": [
            {
                "name": u.name,
                "old": u.old_version,
                "new": u.new_version,
                "type": u.update_type,
                "risk": u.risk_level,
                "auto_mergeable": u.auto_mergeable,
                "pr_url": u.pr_url,
            }
            for u in updates
        ]
    }
