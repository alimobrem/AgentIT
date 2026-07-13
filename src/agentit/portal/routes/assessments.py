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

from agentit.audit import audit_log
from agentit.cloner import clone_repo
from agentit.models import AssessmentReport, Severity
from agentit.portal.cluster_apply import apply_manifests_to_cluster, install_operator
from agentit.portal.github_pr import create_onboarding_pr
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
):
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=get_llm_client(), infra_repo_url=infra_repo_url,
            check_results_out=check_results_out,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


def _assess_sync(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
):
    """Run assessment synchronously. Used by webhooks and background threads."""
    infra = infra_repo_url
    if not infra:
        infra = _auto_create_infra_repo(repo_url)
    return _clone_assess_cleanup(repo_url, criticality, infra, check_results_out=check_results_out)


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
    # The work below runs in a background thread (long clone+assess pipeline),
    # so it needs a synchronous store handle -- see helpers.run_onboarding's
    # docstring / docs/postgres-migration-plan.md for why this is the
    # established pattern for background-thread store access rather than
    # awaiting the async facade from inside a non-async thread.
    raw = s.raw if hasattr(s, "raw") else None
    if raw is None:
        raise HTTPException(500, "Background assessment jobs require the sqlite backend's synchronous handle "
                                  "(store_pg.AssessmentStore has none yet) -- see docs/postgres-migration-plan.md")

    import threading

    def _run():
        try:
            raw.update_assessment_job(job_id, "cloning", "Cloning repository...")
            raw.update_assessment_job(job_id, "assessing", "Analyzing repository...")
            check_results: list[dict] = []
            report = _assess_sync(repo_url, criticality, infra, check_results_out=check_results)
            raw.update_assessment_job(job_id, "saving", "Saving results...")
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="success").inc()
            assessment_id = raw.save(report)
            raw.save_check_results(assessment_id, check_results)
            # Publish event on first assessment for this repo
            history = raw.list_history(report.repo_url)
            if len(history) <= 1:
                publish_event(
                    'first-assessment', report.repo_name,
                    f'First assessment — consider running: agentit learn-for {report.repo_url}',
                    {'assessment_id': assessment_id, 'score': report.overall_score},
                    correlation_id=assessment_id,
                )
            raw.update_assessment_job(job_id, "completed", "Assessment complete", assessment_id=assessment_id)
        except Exception as exc:
            log.exception("Assessment failed for %s", repo_url)
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="error").inc()
            msg = str(exc)
            if "clone" in msg.lower() or "git" in msg.lower():
                msg = f"Could not clone repository. Check the URL and permissions. ({msg[:100]})"
            elif "GITHUB_TOKEN" in msg:
                msg = "GitHub integration is not configured. Contact your administrator."
            raw.update_assessment_job(job_id, "failed", msg[:200])

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
    try:
        report = await with_timeout(
            asyncio.to_thread(
                _clone_assess_cleanup, repo_url, "high", infra, check_results_out=check_results,
            )
        )
    except Exception as exc:
        log.exception("Self-assessment failed")
        return RedirectResponse(url=f"/?error={quote(str(exc)[:200])}", status_code=303)
    s = await get_store()
    assessment_id = await s.save(report)
    await s.save_check_results(assessment_id, check_results)
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
    fixable_categories = {f.category for f in urgent_findings if lookup(f.category) is not None}

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
            "timeline": timeline,
            "trend": trend,
            "score_history": score_history,
            "lifecycle_stage": lifecycle_stage,
            "suppressions": suppressions,
        },
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

    # RemediationDispatcher is deliberately still fully synchronous (see
    # docs/postgres-migration-plan.md's Phase 3 progress notes), so it needs
    # the raw sync store handle, run off the event loop via to_thread.
    raw = s.raw if hasattr(s, "raw") else None
    if raw is None:
        raise HTTPException(500, "Fix dispatch requires the sqlite backend's synchronous handle "
                                  "(store_pg.AssessmentStore has none yet) -- see docs/postgres-migration-plan.md")

    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(raw)
    result = await asyncio.to_thread(dispatcher.dispatch, assessment_id, category, report.repo_name)

    from agentit.portal.metrics import remediations_total as _rt
    _status = "success" if result["files"] else ("error" if result.get("error") else "empty")
    _rt.labels(agent=result.get("agent", "unknown"), status=_status).inc()

    if result.get("error") and not result["files"]:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}?error={quote(result['error'])}",
            status_code=303,
        )

    if result["files"]:
        from agentit.portal.cluster_apply import apply_manifests_to_cluster
        namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
        apply_result = await asyncio.to_thread(
            apply_manifests_to_cluster, result["files"], namespace, dry_run=True,
        )
        await s.save_apply_results(assessment_id, apply_result, namespace, dry_run=True)

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


def _run_onboarding(
    report: AssessmentReport, assessment_id: str | None = None, raw_store: object | None = None,
) -> tuple[list[dict], dict]:
    """Run orchestrated onboarding. Returns (files, orchestration_summary).

    Delegates to the shared implementation in helpers.py so this route and
    the webhook-triggered path (routes/webhooks.py) can never drift apart on
    which summary fields get stored (e.g. auto_approve/gates).

    Runs inside a worker thread via ``asyncio.to_thread`` (see the caller
    below) -- ``raw_store`` must therefore already be the *synchronous*
    store handle (``FleetOrchestrator`` is deliberately still fully
    synchronous; see docs/postgres-migration-plan.md's Phase 3 notes), not
    the async facade `get_store()` now returns.
    """
    from agentit.portal.helpers import run_onboarding as _shared_run_onboarding
    return _shared_run_onboarding(report, assessment_id, store=raw_store)


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

    raw = s.raw if hasattr(s, "raw") else None
    if raw is None:
        raise HTTPException(500, "Onboarding requires the sqlite backend's synchronous handle "
                                  "(store_pg.AssessmentStore has none yet) -- see docs/postgres-migration-plan.md")
    try:
        files, orch_summary = await with_timeout(asyncio.to_thread(_run_onboarding, report, assessment_id, raw))
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


@router.post("/assessments/{assessment_id}/apply", response_model=None)
async def apply_to_cluster(request: Request, assessment_id: str):
    """Apply onboarding manifests to the current cluster."""
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    form = await request.form()
    namespace = str(form.get("namespace", "default"))
    dry_run = form.get("dry_run") == "true"

    try:
        results = await asyncio.to_thread(
            apply_manifests_to_cluster, files, namespace, dry_run,
        )
    except Exception:
        log.exception("Cluster apply failed for assessment %s", assessment_id)
        audit_log(actor=get_current_user(request), action="apply-to-cluster", resource=f"assessment:{assessment_id}",
                  outcome="error", details={"namespace": namespace, "dry_run": dry_run})
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote('Cluster apply failed — check server logs')}",
            status_code=303,
        )

    await s.save_apply_results(assessment_id, results, namespace, dry_run)

    applied = len(results["applied"])
    skipped = len(results["skipped"])
    errs = len(results["errors"])
    audit_log(actor=get_current_user(request), action="apply-to-cluster", resource=f"assessment:{assessment_id}",
              outcome="success" if not results["errors"] else "partial",
              details={"namespace": namespace, "dry_run": dry_run, "applied": applied, "errors": errs})
    return RedirectResponse(
        url=(
            f"/assessments/{assessment_id}/onboard-results"
            f"?applied={applied}&skipped={skipped}&errors={errs}"
            f"&dry_run={'true' if dry_run else 'false'}"
        ),
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


@router.post("/assessments/{assessment_id}/create-pr", response_model=None)
async def create_pr(assessment_id: str):
    """Commit manifests to GitOps infra repo (or app repo as fallback)."""
    from agentit.portal.github_pr import commit_to_infra_repo, ensure_applicationset

    s = await get_store()
    report = await s.get(assessment_id)
    files = await s.get_onboarding(assessment_id)
    if report is None or files is None:
        raise HTTPException(status_code=404, detail="Assessment or onboarding not found")

    try:
        if report.infra_repo_url:
            result = await with_timeout(asyncio.to_thread(
                commit_to_infra_repo, report.infra_repo_url, report.repo_name, files,
            ))
            await asyncio.to_thread(ensure_applicationset, report.infra_repo_url)
        else:
            result = await with_timeout(asyncio.to_thread(
                create_onboarding_pr, report.repo_url, report.repo_name, files,
            ))
    except Exception as exc:
        log.exception("PR creation failed for %s", report.repo_name)
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(str(exc)[:200])}",
            status_code=303,
        )

    if "error" in result:
        log.warning("PR creation error for %s: %s", report.repo_name, result["error"])
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(result['error'][:200])}",
            status_code=303,
        )
    await s.update_pr_url(assessment_id, result["pr_url"])
    await s.log_event("portal", "pr-created", report.repo_name,
                       "info", f"PR created: {result['pr_url']}")
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?pr_url={result['pr_url']}",
        status_code=303,
    )


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
