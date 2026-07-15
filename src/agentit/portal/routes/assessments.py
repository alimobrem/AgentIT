"""Assessment lifecycle: create, view, onboard, apply, and PR creation."""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
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


def _assess_sync(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
    secret_decisions_out: list[dict] | None = None,
):
    """Run assessment synchronously. Used by webhooks and background threads."""
    infra = infra_repo_url
    if not infra:
        infra = _auto_create_infra_repo(repo_url)
    return _clone_assess_cleanup(
        repo_url, criticality, infra,
        check_results_out=check_results_out, secret_decisions_out=secret_decisions_out,
    )


@router.get("/assess")
async def assess_form():
    """Redirect to fleet with modal open — single entry point for assessment."""
    return RedirectResponse(url="/?assess=1", status_code=303)


@router.post("/assess", response_model=None)
async def assess_submit(
    request: Request,
    repo_url: str = Form(...),
    criticality: str = Form("medium"),
    infra_repo_url: str = Form(""),
):
    infra = infra_repo_url.strip() or None
    s = await get_store()
    job_id = await s.create_assessment_job(repo_url)
    # The work below runs in a background thread (long clone+assess pipeline)
    # via a plain `threading.Thread`, not `asyncio.to_thread` -- unlike
    # `to_thread` (awaited by the caller before the request finishes), this
    # thread keeps running after the redirect below is returned, so the
    # request coroutine can't stick around to `await` anything on its
    # behalf.
    #
    # Sqlite: `s.raw` hands back a genuinely synchronous, thread-safe store
    # handle with no event-loop dependency at all -- every call in `_run()`
    # below just calls straight through it, exactly as before this fix.
    #
    # Postgres: `store_pg.AssessmentStore` has no `.raw` on purpose (see
    # docs/postgres-migration-plan.md §7 -- handing a synchronous-only
    # consumer a Postgres-backed store is exactly the silent partial-cutover
    # that must fail loudly instead). Its coroutine methods are bridged back
    # onto *this* coroutine's event loop via `asyncio.run_coroutine_threadsafe`
    # -- the exact pattern `EventConsumer._persist_dead_letter` established
    # in commit 7533309 for the same underlying constraint: an `asyncpg`
    # connection pool is bound to the event loop that created it and can't
    # be driven from a different thread's loop. This only works as long as
    # that loop stays alive for the duration of the background thread (true
    # for the portal's real, persistent uvicorn event loop; a test harness
    # that tears its loop down per-request must exercise this path with its
    # own long-lived loop -- see tests/test_watcher_cli_postgres.py's pattern).
    raw = s.raw if hasattr(s, "raw") else None
    loop = asyncio.get_running_loop() if raw is None else None
    store = raw if raw is not None else s

    import threading

    def _bridge(result):
        """Sqlite: `result` is already the real value (`store` is `raw`, a
        plain sync call) -- passthrough. Postgres: `result` is the
        coroutine `store.<method>(...)` constructed but not yet run --
        schedule it onto `loop` and block this worker thread until done."""
        if raw is not None or not asyncio.iscoroutine(result):
            return result
        return asyncio.run_coroutine_threadsafe(result, loop).result(timeout=60)

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
async def assess_progress(request: Request, job_id: str):
    s = await get_store()
    job = await s.get_remediation_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job["status"] == "completed" and job.get("assessment_id"):
        return RedirectResponse(url=f"/assessments/{job['assessment_id']}", status_code=303)

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


@router.post("/self-assess", response_model=None)
async def self_assess_route(request: Request):
    """One-click self-assessment -- AgentIT assesses its own repo."""
    repo_url = "https://github.com/alimobrem/AgentIT"
    infra = await asyncio.to_thread(_auto_create_infra_repo, repo_url)
    check_results: list[dict] = []
    secret_decisions: list[dict] = []
    try:
        report = await with_timeout(
            asyncio.to_thread(
                _clone_assess_cleanup, repo_url, "high", infra,
                check_results_out=check_results, secret_decisions_out=secret_decisions,
            )
        )
    except Exception as exc:
        log.exception("Self-assessment failed")
        return RedirectResponse(url=f"/?error={quote(str(exc)[:200])}", status_code=303)
    s = await get_store()
    assessment_id = await s.save(report)
    await s.save_check_results(assessment_id, check_results)
    from agentit.llm_decisions import build_secret_classify_events
    for ev in build_secret_classify_events(secret_decisions, report.repo_name):
        await s.log_event(**ev, correlation_id=assessment_id)
    await s.log_event("self-assess", "assessment-complete", "agentit", "info",
                       f"Self-assessment complete: {report.overall_score:.0f}/100")
    from agentit.events import TOPIC_ASSESSMENTS as _TOPIC_ASSESS
    publish_event("assessment-complete", "agentit",
                   f"Self-assessment: {report.overall_score:.0f}/100",
                   {"assessment_id": assessment_id, "score": report.overall_score},
                   correlation_id=assessment_id,
                   extra_topic=_TOPIC_ASSESS)
    return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)


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

    remediations = await s.list_remediations(assessment_id)
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

    # The 7 app-owner-scoped gate types now live here (Actions tab) instead
    # of the retired global Gates page -- cluster-admin-review is excluded,
    # it's the one gate type for a genuinely different audience and stays
    # on the separate Admin Review page (docs/ui-redesign-proposal.md §2).
    from agentit.portal.delivery import ADMIN_REVIEW_GATE_TYPE, gate_delivery_confirmation, is_gitops_registered
    assessment_gates = await s.list_gates_for_assessment(assessment_id, status="pending") \
        if hasattr(s, "list_gates_for_assessment") else []
    pending_actions = [g for g in assessment_gates if g["gate_type"] != ADMIN_REVIEW_GATE_TYPE]
    for g in pending_actions:
        g["delivery_confirmation"] = await gate_delivery_confirmation(s, g)

    gitops_registered, infra_repo_url = await is_gitops_registered(report.repo_name, report)

    timeline = await s.get_assessment_timeline(assessment_id) if hasattr(s, 'get_assessment_timeline') else []
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

    suppressions = await s.get_suppressions(report.repo_name)

    return get_templates().TemplateResponse(
        request,
        "assessment_detail.html",
        {
            "report": report,
            "scores_sorted": scores_sorted,
            "urgent_findings": urgent_findings,
            "assessment_id": assessment_id,
            "remediation_count": len(remediations),
            "slo_count": len(slos),
            "onboarding_count": len(onboardings),
            "fixable_categories": fixable_categories,
            "pending_actions": pending_actions,
            "gitops_registered": gitops_registered,
            "infra_repo_url": infra_repo_url,
            "timeline": timeline,
            "trend": trend,
            "score_history": score_history,
            "lifecycle_stage": lifecycle_stage,
            "suppressions": suppressions,
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
                url=f"/assessments/{assessment_id}?error={quote('Could not auto-create a GitOps infra repo — check GitHub token permissions')}",
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
        # unified delivery router for a GitOps-registered app.
        for f in result["files"]:
            await s.save_remediation(
                assessment_id, result["agent"], f["description"],
                status="generated", manifest_path=f["path"],
            )
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


@router.post("/assessments/{assessment_id}/onboard", response_model=None)
async def onboard_submit(request: Request, assessment_id: str):
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    try:
        files, orch_summary = await with_timeout(_run_onboarding(report, assessment_id, s))
    except HTTPException:
        raise
    except Exception:
        log.exception("Onboarding failed for %s", assessment_id)
        from agentit.portal.metrics import onboardings_total as _ot
        _ot.labels(status="error").inc()
        return RedirectResponse(
            url=f"/assessments/{assessment_id}?error={quote('Onboarding failed — check server logs')}",
            status_code=303,
        )
    from agentit.portal.metrics import onboardings_total as _ot
    _ot.labels(status="success").inc()
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

    from agentit.portal.github_pr import ensure_webhook
    webhook_url = _get_trusted_base_url(request) + "/api/webhook/github-push"
    hook_result = await asyncio.to_thread(ensure_webhook, report.repo_url, webhook_url)
    if "error" in hook_result:
        log.warning("Webhook registration failed for %s: %s", report.repo_name, hook_result["error"])
        warnings.append(f"Auto-reassessment webhook not registered: {hook_result['error'][:100]}")
    elif hook_result.get("created"):
        await s.log_event("portal", "webhook-registered", report.repo_name,
                           "info", "GitHub push webhook registered for auto-reassessment")

    redirect_url = f"/assessments/{assessment_id}/onboard-results"
    if warnings:
        redirect_url += f"?warning={quote('|'.join(warnings))}"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("/assessments/{assessment_id}/onboard-results", response_class=HTMLResponse)
async def onboard_results(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

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
        MECHANISM_DIRECT_APPLY,
        MECHANISM_INFRA_REPO_COMMIT,
        confirmation_text,
        is_gitops_registered,
    )
    gitops_registered, infra_repo_url = await is_gitops_registered(report.repo_name, report)
    delivery_mechanism = MECHANISM_INFRA_REPO_COMMIT if gitops_registered else MECHANISM_DIRECT_APPLY
    delivery_confirmation = confirmation_text(delivery_mechanism, infra_repo_url=infra_repo_url)
    deliveries = await s.list_deliveries(assessment_id) if hasattr(s, "list_deliveries") else []

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
    from agentit.portal.delivery import route_and_deliver

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

    try:
        delivery = await route_and_deliver(
            files, app_name=report.repo_name, namespace=namespace,
            report=report, store=s, assessment_id=assessment_id,
            actor=get_current_user(request), dry_run=dry_run,
            force_dry_run_first=False,
        )
    except Exception:
        log.exception("Delivery failed for assessment %s", assessment_id)
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote('Delivery failed — check server logs')}",
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
    if errors:
        params.append(f"error={quote(' | '.join(errors)[:300])}")
    if dry_run_previews:
        params.append(f"dry_run_summary={quote(' + '.join(dry_run_previews))}")
    if outcomes.get("cicd_shared_namespace"):
        params.append("cicd_gate=true")
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
    """Suppress a check for a specific app — it won't fire on future assessments."""
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    check_source = str(form.get("check_source", ""))
    reason = str(form.get("reason", ""))
    assessment_id = str(form.get("assessment_id", ""))
    if not app_name or not check_source:
        raise HTTPException(status_code=400, detail="app_name and check_source required")
    s = await get_store()
    await s.suppress_check(app_name, check_source, reason)
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

    if successful:
        pr_list = ", ".join(f"{r['agent_name']}" for r in successful)
        all_pr_urls = " | ".join(r["pr_url"] for r in successful)
        await s.update_pr_url(assessment_id, all_pr_urls)
        await s.log_event("orchestrator", "agent-prs-created", report.repo_name,
                           "info", f"Created {len(successful)} per-agent PRs: {pr_list}")

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
