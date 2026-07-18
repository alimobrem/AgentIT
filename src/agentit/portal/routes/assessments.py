"""Assessment lifecycle: create, view, onboard, apply, and PR creation."""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from agentit.cloner import clone_repo
from agentit.models import AssessmentReport, Severity
from agentit.portal.cluster_apply import install_operator
from agentit.portal.helpers import get_current_user, get_llm_client, get_store, get_templates, publish_event, with_timeout
from agentit.runner import run_assessment

log = logging.getLogger(__name__)

router = APIRouter()


def _get_trusted_base_url(request: Request) -> str:
    """This app's own externally-reachable base URL, for building outbound URLs
    (e.g. the GitHub webhook registration below) that we hand to third parties.

    Deliberately does NOT use `request.base_url` as the primary source: that's
    derived from the client-supplied Host header, so a forged Host would make
    us register a webhook pointing at an attacker-controlled server. Prefer an
    explicit trusted override, then our own OpenShift Route (a cluster-side,
    not client-side, source of truth). Only falls back to the request's Host
    header if neither is available (e.g. local dev with no Route).
    """
    import os
    override = os.environ.get("AGENTIT_EXTERNAL_URL")
    if override:
        return override.rstrip("/")
    # Only attempt the Route lookup when actually running in-cluster (the
    # standard KUBERNETES_SERVICE_HOST env var Kubernetes injects into every
    # pod) -- otherwise this would fall through to a real, possibly slow or
    # unreachable, kubeconfig-based cluster on the developer's machine (e.g.
    # in local dev/tests) for every request, instead of a fast, correct
    # no-op that lands on the same request.base_url fallback anyway.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        try:
            from agentit import kube
            namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
            routes = kube.list_custom_resources("route.openshift.io", "v1", "routes", namespace)
            for route in routes:
                if route.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "agentit":
                    host = route.get("spec", {}).get("host")
                    if host:
                        return f"https://{host}"
        except Exception:
            log.warning("Could not resolve own Route for trusted base URL; "
                        "falling back to request Host header", exc_info=True)
    return str(request.base_url).rstrip("/")


def _clone_assess_cleanup(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
    secret_decisions_out: list[dict] | None = None,
):
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


class InfraRepoRequiredError(Exception):
    """Raised when a real GitOps infra repo can't be resolved for a new
    assessment -- the product directive that all apps must use GitOps means
    this is a hard stop on Assess, never a fallback to Direct Apply. Carries
    a human-readable, actionable message (trusted-domain rejection, a
    repo-creation permission error, etc.) shown verbatim on the failed job.
    """


def _resolve_mandatory_infra_repo_url(repo_url: str, human_supplied: str | None) -> str:
    """Resolve a real, usable GitOps infra repo URL for a brand-new
    assessment -- auto-created via ``_auto_create_infra_repo()`` when the
    human didn't supply one, otherwise the human-supplied URL itself.
    Either way the result is validated against the same trusted-git-host
    allowlist ``ensure_applicationset()`` enforces at first-delivery time
    (``github_pr.is_trusted_git_host()``), so an untrusted or unusable infra
    repo is rejected here -- at Assess time -- rather than silently accepted
    only to discover, much later, that GitOps sync will never actually work.

    Raises ``InfraRepoRequiredError`` (never returns ``None``/falls back to
    Direct Apply) on any failure -- all apps must be GitOps-registered now.
    """
    from agentit.portal.github_pr import is_trusted_git_host

    if human_supplied:
        if not is_trusted_git_host(human_supplied):
            raise InfraRepoRequiredError(
                f"GitOps infra repo '{human_supplied}' is not on a trusted Git host "
                "(set AGENTIT_TRUSTED_GIT_DOMAINS if it should be) -- Assess cannot "
                "proceed without a usable GitOps infra repo."
            )
        return human_supplied

    infra = _auto_create_infra_repo(repo_url)
    if infra is None:
        raise InfraRepoRequiredError(
            "Could not auto-create a GitOps infra repo for this app (often a "
            "GITHUB_TOKEN permissions issue, or the repo's GitHub org/token doesn't "
            "allow AgentIT to create a private repo there) -- all apps must be "
            "GitOps-registered now, with no Direct Apply fallback. Supply a GitOps "
            "Infra Repo URL manually and retry Assess."
        )
    if not is_trusted_git_host(infra):
        # Nothing in the request handed us this URL -- it came back from our
        # own _auto_create_infra_repo()/GitHub API call -- so this branch is
        # only reachable if AGENTIT_TRUSTED_GIT_DOMAINS was narrowed below the
        # default GitHub host ensure_infra_repo() itself always creates
        # against. Still validated (never assumed) rather than skipped.
        raise InfraRepoRequiredError(
            f"Auto-created GitOps infra repo '{infra}' is not on a trusted Git host -- "
            "Assess cannot proceed without a usable GitOps infra repo."
        )
    return infra


def _assess_sync(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
    secret_decisions_out: list[dict] | None = None,
):
    """Run assessment synchronously. Used by webhooks and background threads.

    GitOps registration is mandatory: resolves (and validates) a real
    infra_repo_url BEFORE cloning/running the assessment pipeline at all --
    see ``_resolve_mandatory_infra_repo_url()``. Raises
    ``InfraRepoRequiredError`` (a hard stop, no Direct Apply fallback) if none
    can be resolved, so the caller never wastes a clone+assess cycle on an
    app that can't proceed anyway.
    """
    infra = _resolve_mandatory_infra_repo_url(repo_url, infra_repo_url)
    return _clone_assess_cleanup(
        repo_url, criticality, infra,
        check_results_out=check_results_out, secret_decisions_out=secret_decisions_out,
    )


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
    # just Fleet's "Re-scan" button (docs/onboarding-loop-vision-
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
    job_id = await s.create_assessment_job(repo_url, continue_onboard=chain)
    # The work below runs in a background thread (long clone+assess pipeline)
    # via a plain `threading.Thread`, not `asyncio.to_thread` -- unlike
    # `to_thread` (awaited by the caller before the request finishes), this
    # thread keeps running after the redirect below is returned, so the
    # request coroutine can't stick around to `await` anything on its
    # behalf.
    #
    # `AssessmentStore`'s `asyncpg` connection pool is bound to the event
    # loop that created it and can't be driven from a different thread's
    # loop, so every store call made from this background thread is
    # scheduled back onto *this* coroutine's event loop via
    # `asyncio.run_coroutine_threadsafe` -- the same pattern
    # `EventConsumer._persist_dead_letter` uses for the identical
    # constraint. This only works as long as that loop stays alive for the
    # duration of the background thread (true for the portal's real,
    # persistent uvicorn event loop; a test harness that tears its loop
    # down per-request must exercise this path with its own long-lived loop
    # -- see tests/test_watcher_cli_postgres.py's pattern).
    loop = asyncio.get_running_loop()
    store = s

    import threading

    def _bridge(coro):
        """Schedule a coroutine `store.<method>(...)` call onto `loop` and
        block this worker thread until it completes."""
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)

    def _run():
        try:
            _bridge(store.update_assessment_job(job_id, "cloning", "Cloning repository..."))
            _bridge(store.update_assessment_job(job_id, "assessing", "Analyzing repository..."))
            check_results: list[dict] = []
            secret_decisions: list[dict] = []
            report = _assess_sync(
                repo_url, criticality, infra,
                check_results_out=check_results, secret_decisions_out=secret_decisions,
            )
            _bridge(store.update_assessment_job(job_id, "saving", "Saving results..."))
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="success").inc()
            assessment_id = _bridge(store.save(report))
            _bridge(store.save_check_results(assessment_id, check_results))
            from agentit.llm_decisions import build_secret_classify_events
            for ev in build_secret_classify_events(secret_decisions, report.repo_name):
                _bridge(store.log_event(**ev, correlation_id=assessment_id))
            # `_assess_sync()` now guarantees `report.infra_repo_url` is always
            # set (`_resolve_mandatory_infra_repo_url()` raises
            # `InfraRepoRequiredError` -- handled below -- rather than ever
            # returning `None`) -- there is no more silent-failure/Direct-Apply-
            # fallback case to detect and flag here.
            # Publish event on first assessment for this repo
            history = _bridge(store.list_history(report.repo_url))
            if len(history) <= 1:
                publish_event(
                    'first-assessment', report.repo_name,
                    f'First assessment — consider running: agentit learn-for {report.repo_url}',
                    {'assessment_id': assessment_id, 'score': report.overall_score},
                    correlation_id=assessment_id,
                )
            _bridge(store.update_assessment_job(job_id, "completed", "Assessment complete", assessment_id=assessment_id))
        except InfraRepoRequiredError as exc:
            # A hard stop, never a fallback to Direct Apply -- all apps must
            # be GitOps-registered now. Reuses the visible-failure event
            # pattern 9e036d9 introduced for the (formerly soft-warning)
            # infra-repo-creation-failed case, but no assessment was saved
            # here at all (this fires before the clone+assess pipeline ever
            # runs), so there's no assessment_id/report to correlate to or
            # show a banner on -- the failed job page itself (assess_progress.html)
            # is where this human-readable, actionable message surfaces.
            log.warning("Assess blocked for %s: %s", repo_url, exc)
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="error").inc()
            from agentit.portal.github_pr import _parse_owner_repo
            try:
                _, app_name = _parse_owner_repo(repo_url)
            except Exception:
                app_name = None
            _bridge(store.log_event(
                "portal", "infra-repo-creation-failed", app_name, "critical",
                f"Assess blocked for {repo_url}: {exc}",
            ))
            _bridge(store.update_assessment_job(job_id, "failed", str(exc)[:280]))
        except Exception as exc:
            log.exception("Assessment failed for %s", repo_url)
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="error").inc()
            msg = str(exc)
            if "clone" in msg.lower() or "git" in msg.lower():
                msg = f"Could not clone repository. Check the URL and permissions. ({msg[:100]})"
            elif "GITHUB_TOKEN" in msg:
                msg = "GitHub integration is not configured. Contact your administrator."
            _bridge(store.update_assessment_job(job_id, "failed", msg[:200]))

    threading.Thread(target=_run, daemon=True).start()
    return RedirectResponse(url=f"/assess/progress/{job_id}", status_code=303)


@router.get("/assess/progress/{job_id}", response_class=HTMLResponse)
async def assess_progress(
    request: Request, job_id: str, background_tasks: BackgroundTasks,
):
    s = await get_store()
    job = await s.get_remediation_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job["status"] == "completed" and job.get("assessment_id"):
        assessment_id = job["assessment_id"]
        # Atomically claim continue_onboard so the htmx 2s poll cannot
        # start two onboarding jobs for the same completed assess.
        if hasattr(s, "claim_continue_onboard") and await s.claim_continue_onboard(job_id):
            # auto_deliver=True: the assess->onboard chain always continues
            # into the automatic Dry Run -> Deliver chain too (matches
            # onboard_submit()'s own default -- see its docstring), so the
            # full chain this session's product owner asked for --
            # Assess -> Onboard -> Dry Run -> Deliver (PR opened) -- has no
            # human click anywhere in the middle.
            onboard_job_id = await s.create_remediation_job(assessment_id, auto_deliver=True)
            base_url = _get_trusted_base_url(request)
            background_tasks.add_task(
                _run_onboarding_job, onboard_job_id, assessment_id, base_url, True,
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard/progress/{onboard_job_id}",
                status_code=303,
            )
        return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)

    return get_templates().TemplateResponse(request, "assess_progress.html", {
        "job": job, "job_id": job_id,
    })


def _auto_create_infra_repo(repo_url: str) -> str | None:
    """Auto-create a GitOps infra repo based on the app repo owner."""
    try:
        from agentit.portal.github_pr import _parse_owner_repo, ensure_infra_repo
        owner, _ = _parse_owner_repo(repo_url)
        result = ensure_infra_repo(owner)
        if "repo_url" in result:
            log.info("Infra repo: %s (created=%s)", result["repo_url"], result.get("created", False))
            return result["repo_url"]
        log.warning("Failed to create infra repo: %s", result.get("error"))
    except Exception as exc:
        log.warning("Auto-create infra repo failed: %s", exc)
    return None


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

    from agentit.remediation.registry import lookup
    # Computed from every finding (not just urgent_findings' critical/high
    # subset) so the same set correctly covers the Remediation Plan table
    # below, which lists findings of every severity -- one source of truth
    # for "is this finding/plan-item fixable", shared by both loops in the
    # template instead of each re-deriving visibility independently
    # (docs/ui-redesign-proposal.md §0's second bug).
    all_findings = [f for sc in report.scores for f in sc.findings]
    fixable_categories = {f.category for f in all_findings if lookup(f.category) is not None}

    # Every gate type now lives here (Actions tab) instead of the retired
    # global Gates page -- including a stale ``cluster-admin-review`` row, if
    # one somehow still exists (that type, and the separate Admin Review
    # page it used to live on, were retired 2026-07-18 -- see delivery.py).
    from agentit.portal.delivery import (
        gate_delivery_confirmation,
        get_next_action_state,
        is_gitops_registered,
    )
    assessment_gates = await s.list_gates_for_assessment(assessment_id, status="pending") \
        if hasattr(s, "list_gates_for_assessment") else []
    pending_actions = assessment_gates
    for g in pending_actions:
        g["delivery_confirmation"] = await gate_delivery_confirmation(s, g)

    # Real "what happens next" fact (docs/onboarding-loop-vision-gap-
    # analysis.md's Step 8 discussion / Phase 5) -- reuses `assessment_gates`
    # (already fetched above, already scoped to this app) instead of a
    # second gates query.
    next_action = await get_next_action_state(
        s, report.repo_name, repo_url=report.repo_url, pending_gates=assessment_gates,
    )

    # Real, specific empty-state copy for the Actions tab (docs/ux-design-
    # requirements.md checklist #10) -- how many of THIS app's gates were
    # actually resolved recently, instead of a bare "nothing here".
    recently_resolved_actions_count = 0
    if not pending_actions and hasattr(s, "list_gates_for_assessment"):
        all_app_gates = await s.list_gates_for_assessment(assessment_id)
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recently_resolved_actions_count = sum(
            1 for g in all_app_gates
            if g["status"] in ("approved", "rejected", "expired", "cancelled")
            and (g.get("resolved_at") or g.get("created_at") or "") >= cutoff
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

    timeline = await s.get_assessment_timeline(assessment_id) if hasattr(s, 'get_assessment_timeline') else []
    # docs/ledger-design-spec.md Phase 1: additive 5th tab, alongside (not
    # replacing) Actions/Timeline above -- same gate_card macro, same
    # route_and_deliver()/resolve_gate() paths, nothing existing changes.
    from agentit.ledger import get_ledger_cards
    ledger_cards = await get_ledger_cards(s, target_app=report.repo_name, assessment_id=assessment_id)
    trend = await s.get_trend(report.repo_url) if hasattr(s, 'get_trend') else {}
    score_history = await s.get_score_history(report.repo_url) if hasattr(s, 'get_score_history') else []
    for i, h in enumerate(score_history):
        h["delta"] = round(h["overall_score"] - score_history[i - 1]["overall_score"], 2) if i > 0 else None
    apply_results = await s.get_apply_results(assessment_id)

    app_name = report.repo_name
    schedules_exist = await s.has_schedules_for_app(app_name) if hasattr(s, 'has_schedules_for_app') else False
    if apply_results and apply_results.get("applied") and (slos or schedules_exist):
        lifecycle_stage = "monitored"
    elif apply_results and apply_results.get("applied"):
        lifecycle_stage = "applied"
    elif onboardings:
        lifecycle_stage = "onboarded"
    else:
        lifecycle_stage = "assessed"

    # True when an older assessment of this repo already onboarded — so a
    # fresh "assessed" stage after Re-assess is a refresh, not first-time.
    prior_onboarded = False
    if lifecycle_stage == "assessed" and hasattr(s, "repo_has_onboarding"):
        prior_onboarded = await s.repo_has_onboarding(report.repo_url)

    suppressions = await s.get_suppressions(report.repo_name)

    # A genuine, real milestone (docs/ux-design-requirements.md checklist
    # #9) -- the FIRST time this app reaches a perfect score, never on
    # every routine assessment that happens to already be at 100. Derived
    # entirely from this app's own real score history, never fabricated.
    celebrate_first_perfect_score = report.overall_score >= 100 and not any(
        h["overall_score"] >= 100 for h in score_history if h["id"] != assessment_id
    )

    # Open PRs section + PR History tab (real GitHub/DB-backed data only --
    # see pr_tracking.py's module docstring for exactly what's tracked vs.
    # what still needs a live GitHub call).
    from agentit.portal.pr_tracking import get_app_pr_history
    pr_history = await get_app_pr_history(s, assessment_id, report.repo_url, report.repo_name)
    open_prs = [pr for pr in pr_history if pr["state"] == "open"]

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
            "pending_actions": pending_actions,
            "next_action": next_action,
            "gitops_registered": gitops_registered,
            "infra_repo_url": infra_repo_url,
            "infra_repo_creation_failed": infra_repo_creation_failed,
            "timeline": timeline,
            "ledger_cards": ledger_cards,
            "trend": trend,
            "score_history": score_history,
            "lifecycle_stage": lifecycle_stage,
            "prior_onboarded": prior_onboarded,
            "suppressions": suppressions,
            "celebrate_first_perfect_score": celebrate_first_perfect_score,
            "recently_resolved_actions_count": recently_resolved_actions_count,
            "open_prs": open_prs,
            "pr_history": pr_history,
            "assessment_cadence": assessment_cadence,
            "reassess_scheduler_active": reassess_scheduler_active,
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
            f"{quote('GitOps infra repo configured via ' + infra_repo_url + '. This app will show as GitOps-registered once your next Fix/Onboard delivery is committed and merged there.')}"
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


async def _run_onboarding(
    report: AssessmentReport, assessment_id: str | None = None, store: object | None = None,
) -> tuple[list[dict], dict]:
    """Run orchestrated onboarding. Returns (files, orchestration_summary).

    Delegates to the shared implementation in helpers.py so this route and
    the webhook-triggered path (routes/webhooks.py) can never drift apart on
    which summary fields get stored (e.g. auto_approve/gates).

    ``FleetOrchestrator`` is now genuinely async, so this is a plain
    coroutine `await`ed directly by the caller below -- ``store`` should be
    whatever `get_store()` returned (async-compatible), no more `.raw`/
    `asyncio.to_thread` bridge needed for this call path.
    """
    from agentit.portal.helpers import run_onboarding as _shared_run_onboarding
    return await _shared_run_onboarding(report, assessment_id, store=store)


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


async def _run_onboarding_job(
    job_id: str, assessment_id: str, base_url: str, auto_deliver: bool = False,
) -> None:
    """Background body of onboarding -- runs after ``onboard_submit`` has
    already redirected the human to the real-time progress page below.

    Real, determinate, per-stage progress (docs/ux-design-requirements.md
    Part 3 item 11/checklist #6): each stage transition is a genuine
    checkpoint this function actually reaches, written via the SAME
    ``remediation_jobs`` job-tracking mechanism ``assess_submit()`` already
    uses for assessment progress (``create_remediation_job``/
    ``update_remediation_job``/``get_remediation_job`` -- no new store
    method, no new table). The per-agent breakdown shown on the progress
    page comes from the real events ``FleetOrchestrator._log_event()``
    already writes live, per agent, via ``list_events_by_correlation_id()``
    -- not fabricated here.

    ``auto_deliver`` (default False -- ``onboard_submit()``/the assess-chain
    call site both pass the real, form-resolved flag; only a caller that
    never resolves it stays off) chains the once-manifests-are-saved
    onboarding job straight into the same automatic Dry Run -> Deliver
    sequence a human otherwise clicks by hand on Onboard Results (see
    ``delivery.auto_dry_run_then_deliver()``) -- but only once
    ``AutoMode.should_auto_apply_and_log()`` (the same LLM confidence/
    destructive-action safety check the vuln-watcher/webhook auto-apply
    paths already require) classifies the generated manifests as safe; a
    low-confidence or destructive classification halts at
    ``gated_for_review`` instead of chaining, falling back to requiring an
    explicit human Deliver click. Job status moves through ``dry_run`` ->
    ``delivering`` -> ``completed`` on a clean run; a real Dry Run failure
    (no infra repo known, etc.) halts at ``dry_run_failed`` and a Deliver-
    stage failure at ``deliver_failed`` -- both terminal, both routed to
    Onboard Results (never back to Assessment Detail, since manifests
    already exist by this point) with the failure surfaced, per the "Dry
    Run is a real, respected gate" requirement.

    Uses its own store handle (not the request-scoped one) since this
    coroutine keeps running after the request/response cycle that started
    it (via FastAPI's ``BackgroundTasks``, which awaits it on the same
    event loop after the response is sent -- no cross-thread store
    bridging needed, unlike ``assess_submit()``'s CLI-shared thread path).
    """
    s = await get_store()
    try:
        report = await s.get(assessment_id)
        if report is None:
            await s.update_remediation_job(job_id, "failed", "Assessment not found", error="Assessment not found")
            return
        await s.update_remediation_job(job_id, "running", "Running onboarding agents...")
        files, orch_summary = await with_timeout(_run_onboarding(report, assessment_id, s))

        from agentit.portal.metrics import onboardings_total as _ot
        _ot.labels(status="success").inc()
        await s.update_remediation_job(job_id, "saving", "Saving generated manifests...")
        await s.save_onboarding(assessment_id, files, orchestration=orch_summary)

        publish_event("onboarding-complete", report.repo_name,
                       f"Generated {len(files)} manifests",
                       {"assessment_id": assessment_id, "file_count": len(files)},
                       correlation_id=assessment_id, agent_id="onboarding")

        # Trigger image build only if a Containerfile was generated
        warnings = []
        has_containerfile = any(
            f["path"].lower() in ("containerfile", "dockerfile") for f in files
        )
        if has_containerfile:
            await s.update_remediation_job(job_id, "running", "Triggering container image build...")
            from agentit.image_builder import build_app_image
            build_result = await asyncio.to_thread(build_app_image, report.repo_url, report.repo_name)
            if "error" in build_result:
                log.warning("Image build trigger failed for %s: %s", report.repo_name, build_result["error"])
                await s.log_event("image-builder", "build-failed", report.repo_name, "warning",
                                   f"Image build failed: {build_result['error'][:200]}")
                warnings.append(f"Image build failed: {build_result['error'][:100]}")
            else:
                log.info("Image build triggered: %s → %s", report.repo_name, build_result.get("image_ref"))
                await s.log_event("image-builder", "build-triggered", report.repo_name, "info",
                                   f"Building image: {build_result.get('image_ref')}")

        await s.update_remediation_job(job_id, "running", "Registering re-assessment webhook...")
        from agentit.portal.github_pr import ensure_webhook
        webhook_url = base_url + "/api/webhook/github-push"
        hook_result = await asyncio.to_thread(ensure_webhook, report.repo_url, webhook_url)
        if "error" in hook_result:
            log.warning("Webhook registration failed for %s: %s", report.repo_name, hook_result["error"])
            warnings.append(f"Auto-reassessment webhook not registered: {hook_result['error'][:100]}")
        elif hook_result.get("created"):
            await s.log_event("portal", "webhook-registered", report.repo_name,
                               "info", "GitHub push webhook registered for auto-reassessment")

        # Warnings (image build / webhook registration failures) are already
        # persisted as real events above (visible on this app's Timeline
        # tab and the global Events page) -- not re-threaded through the
        # job's `error` column, which is reserved for a genuine failure.
        _ = warnings

        if not auto_deliver:
            await s.update_remediation_job(job_id, "completed", "Onboarding complete")
            return

        from agentit.portal.delivery import auto_dry_run_then_deliver

        namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
        # Re-fetch from the store (mirrors deliver()'s own pattern) rather
        # than reusing the local `files` var -- `save_onboarding()` is what
        # actually persisted them, and this operates on the exact persisted
        # shape (`edited` defaults False) `route_and_deliver()` expects.
        chain_files = await s.get_onboarding(assessment_id) or []

        # AutoMode's LLM safety classification -- the same confidence-
        # threshold/destructive-action gate the vuln-watcher/webhook auto-
        # apply paths (routes/webhooks.py's webhook_auto_apply/dispatcher
        # loop) already require before opening a real PR. Onboarding's
        # auto_dry_run_then_deliver() below is deliberately narrow (see its
        # own docstring) and never re-decides this on its own, so without
        # this check onboarding's main auto-deliver flow opened a GitOps PR
        # with no LLM review at all. A low-confidence/destructive
        # classification here doesn't fail onboarding -- the manifests
        # already exist -- it just falls back to requiring an explicit
        # human "Deliver" click on Onboard Results instead of auto-opening
        # the PR.
        from agentit.automode import AutoMode

        auto = AutoMode(store=s, publisher=None, llm_client=get_llm_client())
        chain_manifests = [f["content"] for f in chain_files if f["path"].endswith((".yaml", ".yml"))]
        can_auto_deliver, safety_reason = await auto.should_auto_apply_and_log(
            orch_summary.get("auto_approve", False), chain_manifests, report.criticality, report.repo_name,
        )
        if not can_auto_deliver:
            message = (
                f"Automatic delivery skipped after onboarding -- AutoMode's safety check gated "
                f"this batch for human review: {safety_reason}. Review and click Deliver on "
                "Onboard Results to proceed manually."
            )
            await s.log_event(
                "portal", "onboard-auto-deliver-gated", report.repo_name, "warning",
                message, correlation_id=assessment_id,
            )
            await s.update_remediation_job(job_id, "gated_for_review", message[:280])
            return

        async def _on_stage(stage: str) -> None:
            label = {
                "dry_run": "Running automatic Dry Run...",
                "delivering": "Dry Run passed -- committing and opening PR...",
            }[stage]
            await s.update_remediation_job(job_id, stage, label)

        from agentit.assessment_diff import current_finding_keys

        chain_result = await auto_dry_run_then_deliver(
            chain_files, app_name=report.repo_name, namespace=namespace, report=report,
            store=s, assessment_id=assessment_id, actor="onboarding-auto-deliver",
            on_stage=_on_stage, target_findings=sorted(current_finding_keys(report)),
        )

        if chain_result["ok"]:
            pr_note = f" PR opened: {chain_result['pr_url']}" if chain_result.get("pr_url") else " Nothing needed a PR."
            await s.log_event(
                "portal", "onboard-auto-delivered", report.repo_name, "info",
                f"Automatic Dry Run and Deliver succeeded after onboarding.{pr_note}",
                correlation_id=assessment_id,
            )
            await s.update_remediation_job(
                job_id, "completed", f"Onboarding, Dry Run, and Delivery complete.{pr_note}",
            )
        else:
            stage = chain_result["stage"]
            friendly_stage = "Dry Run" if stage == "dry_run" else "Deliver"
            status = "dry_run_failed" if stage == "dry_run" else "deliver_failed"
            err = chain_result.get("error") or "Unknown error"
            message = f"Automatic {friendly_stage} failed after onboarding -- the chain stopped here: {err[:200]}"
            await s.log_event(
                "portal", "onboard-auto-deliver-blocked", report.repo_name, "warning",
                message, correlation_id=assessment_id,
            )
            await s.update_remediation_job(job_id, status, message[:280], error=message[:280])
    except Exception as exc:
        log.exception("Onboarding failed for %s", assessment_id)
        from agentit.portal.metrics import onboardings_total as _ot
        _ot.labels(status="error").inc()
        detail = getattr(exc, "detail", None) or str(exc)
        await s.update_remediation_job(
            job_id, "failed",
            f"Onboarding failed: {str(detail)[:180]}",
            error=f"Onboarding failed: {str(detail)[:180]} — no manifests were generated. "
                  f"Check the repository is reachable and agents/skills ran cleanly, then retry Onboard.",
        )


@router.post("/assessments/{assessment_id}/onboard", response_model=None)
async def onboard_submit(
    request: Request,
    assessment_id: str,
    background_tasks: BackgroundTasks,
    auto_deliver: str = Form("1"),
):
    """Kicks off onboarding as a background job and immediately redirects to
    a real-time progress page (docs/ux-design-requirements.md checklist #6)
    instead of blocking the request for however long agent orchestration
    takes -- mirrors ``assess_submit()``'s existing job-tracking pattern.

    Chaining the completed onboarding job straight into an automatic Dry
    Run -> Deliver is now the default for every Onboard, not just the
    assess->onboard chain -- mirrors ``assess_submit()``'s own
    ``continue_onboard`` convention (f215d13): a caller can still opt out
    by explicitly posting ``auto_deliver=0``/``false``/``""`` -- nothing
    today does, but the mechanism (this Form field) stays available rather
    than being removed outright. Direct callers that bypass FastAPI's Form
    injection get the same non-str-default guard ``assess_submit()`` uses,
    treating that the same as an explicit opt-out rather than silently
    defaulting to True for a caller that never resolved the field at all.
    """
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    auto_deliver_flag = auto_deliver if isinstance(auto_deliver, str) else ""
    chain = auto_deliver_flag.strip().lower() in ("1", "true", "yes", "on")
    job_id = await s.create_remediation_job(assessment_id, auto_deliver=chain)
    base_url = _get_trusted_base_url(request)
    background_tasks.add_task(_run_onboarding_job, job_id, assessment_id, base_url, chain)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard/progress/{job_id}", status_code=303,
    )


# Job statuses that end a human's wait on the progress page -- no further
# polling/SSE ticks matter once one of these is reached. "completed"/
# "failed" predate the automatic Dry Run -> Deliver chain; "dry_run_failed"/
# "deliver_failed" are new (see ``_run_onboarding_job``'s docstring).
# "gated_for_review" is AutoMode's LLM safety check declining to auto-
# deliver (low confidence or a destructive classification) -- distinct from
# the two failure states above: onboarding itself succeeded and nothing
# was attempted or failed, a human just needs to click Deliver by hand.
_ONBOARD_JOB_TERMINAL_STATUSES = ("completed", "failed", "dry_run_failed", "deliver_failed", "gated_for_review")


async def _onboard_terminal_redirect_url(store: object, assessment_id: str, job: dict) -> str:
    """Where a human (or an SSE-driven client-side redirect) lands once an
    onboarding job -- and, when auto-chained, its automatic Dry Run +
    Deliver -- reaches a terminal state. Shared by the direct GET redirect
    (``onboard_progress()``) and the SSE stream's server-rendered fragment
    (``onboard_progress_stream()`` -> ``_onboard_progress_fragment.html``'s
    inline ``<script>``) so the two can never disagree about the URL for
    the same job.

    - ``"failed"`` (onboarding generation itself failed, no manifests
      exist) -> Assessment Detail, with the error flash CLAUDE.md's
      "errors must always be visible" convention requires -- unchanged
      from before the automatic chain existed.
    - ``"dry_run_failed"``/``"deliver_failed"`` (manifests exist; the
      automatic chain halted at a real gate) -> Onboard Results, with the
      same error flash -- a human reviews/retries from there, never
      bounced back to Assessment Detail once there's something real to
      look at (requirement: Dry Run stays a real, respected gate).
    - ``"gated_for_review"`` (manifests exist; AutoMode's LLM safety check
      declined to auto-deliver) -> Onboard Results, with a warning flash
      (not the error flash -- nothing failed) explaining the fix still
      needs a manual Deliver click.
    - ``"completed"`` -> Onboard Results, decorated with ``pr_url``/
      ``pr_url_repo`` (reusing ``repo_kind_for_mechanism()``) when this was
      an auto-chained run that actually opened a PR, so the same green
      flash banner a manual "Commit & Open PR" click produces also appears
      here -- an auto-chained success looks identical to a human-driven
      one, not silently reliant on Delivery History alone.
    """
    if job["status"] == "failed":
        return f"/assessments/{assessment_id}?error={quote(job.get('error') or 'Onboarding failed')}"

    base_url = f"/assessments/{assessment_id}/onboard-results"
    if job["status"] in ("dry_run_failed", "deliver_failed"):
        return f"{base_url}?error={quote(job.get('error') or 'Automatic delivery failed')}"
    if job["status"] == "gated_for_review":
        return f"{base_url}?warning={quote(job.get('current_step') or 'Automatic delivery needs human review')}"

    # "completed" -- only decorated further when this run was auto-chained.
    if "auto_deliver" not in (job.get("steps_completed") or []):
        return base_url
    deliveries = await store.list_deliveries(assessment_id) if hasattr(store, "list_deliveries") else []
    if not deliveries:
        return base_url
    from agentit.portal.delivery import repo_kind_for_mechanism
    outcomes = (deliveries[0].get("details") or {}).get("outcomes", {})
    for o in outcomes.values():
        if isinstance(o, dict) and o.get("pr_url"):
            repo_kind = repo_kind_for_mechanism(o.get("mechanism", ""))
            params = f"pr_url={quote(o['pr_url'])}"
            if repo_kind:
                params += f"&pr_url_repo={repo_kind}"
            return f"{base_url}?{params}"
    return base_url


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
            url=await _onboard_terminal_redirect_url(s, assessment_id, job), status_code=303,
        )
    agent_steps = await _onboard_agent_steps(s, assessment_id)
    return get_templates().TemplateResponse(request, "onboard_progress.html", {
        "job": job, "job_id": job_id, "assessment_id": assessment_id, "agent_steps": agent_steps,
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
            redirect_url = await _onboard_terminal_redirect_url(s, assessment_id, job) if is_terminal else None
            html = templates.get_template("_onboard_progress_fragment.html").render(
                job=job, agent_steps=agent_steps, assessment_id=assessment_id, redirect_url=redirect_url,
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
        f"Edited generated file before delivery: {path} — any prior Dry Run/delivery result is "
        "now stale; Dry Run must be re-run against the edited content before Apply/Commit unlocks.",
        correlation_id=assessment_id,
    )
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?edited={quote(path)}",
        status_code=303,
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
        confirmation_text,
        is_gitops_registered,
        resolve_cluster_config_mechanism,
    )
    gitops_registered, infra_repo_url = await is_gitops_registered(report.repo_name, report)
    delivery_mechanism = resolve_cluster_config_mechanism(infra_repo_url)
    delivery_confirmation = confirmation_text(delivery_mechanism, infra_repo_url=infra_repo_url)
    deliveries = await s.list_deliveries(assessment_id) if hasattr(s, "list_deliveries") else []

    # Unlock Commit / Per-Agent when a successful dry-run is persisted, the
    # flash query still carries dry_run_summary (redirect / hx-boost), or a
    # real delivery already completed (keep Override hidden).
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
    from agentit.portal.delivery import repo_kind_for_mechanism, route_and_deliver

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
    try:
        delivery = await route_and_deliver(
            files, app_name=report.repo_name, namespace=namespace,
            report=report, store=s, assessment_id=assessment_id,
            actor=get_current_user(request), dry_run=dry_run,
            target_findings=sorted(current_finding_keys(report)),
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


@router.post("/assessments/{assessment_id}/create-agent-prs", response_model=None)
async def create_agent_prs_route(assessment_id: str):
    """Create per-agent branches and PRs."""
    from agentit.portal.github_pr import create_agent_prs

    s = await get_store()
    report = await s.get(assessment_id)
    files = await s.get_onboarding(assessment_id)
    if report is None or files is None:
        raise HTTPException(status_code=404, detail="Assessment or onboarding not found")

    grouped: dict[str, list[dict]] = {}
    for f in files:
        grouped.setdefault(f["category"], []).append(f)

    agent_results = [
        {"agent_name": cat, "category": cat, "files": cat_files}
        for cat, cat_files in grouped.items()
    ]

    results = await asyncio.to_thread(
        create_agent_prs, report.repo_url, report.repo_name, agent_results,
    )

    successful = [r for r in results if "pr_url" in r]
    errors = [r for r in results if "error" in r]
    skipped = [r for r in results if r.get("skipped")]

    if successful:
        pr_list = ", ".join(f"{r['agent_name']}" for r in successful)
        all_pr_urls = " | ".join(r["pr_url"] for r in successful)
        await s.update_pr_url(assessment_id, all_pr_urls)
        await s.log_event("orchestrator", "agent-prs-created", report.repo_name,
                           "info", f"Created {len(successful)} per-agent PRs: {pr_list}")

    if skipped:
        # Content already matched the target repo's default branch --
        # nothing to commit, so no PR was opened for these agents (see
        # github_pr.py::_agent_content_unchanged).
        skip_list = ", ".join(f"{r['agent_name']}" for r in skipped)
        await s.log_event("orchestrator", "agent-prs-skipped", report.repo_name,
                           "info", f"Skipped {len(skipped)} agent(s) — no changes vs. default branch: {skip_list}")

    if errors and not successful:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(errors[0].get('error', 'Unknown')[:200])}",
            status_code=303,
        )

    pr_urls = "|".join(f"{r['agent_name']}={r['pr_url']}" for r in successful)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?agent_prs={quote(pr_urls)}",
        status_code=303,
    )


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
