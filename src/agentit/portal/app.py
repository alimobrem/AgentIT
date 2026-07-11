from __future__ import annotations

import asyncio
import io
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse
from fastapi.templating import Jinja2Templates

from agentit.cloner import clone_repo
from agentit.models import AssessmentReport, Severity
from agentit.portal.cluster_apply import apply_manifests_to_cluster, install_operator
from agentit.portal.github_pr import create_onboarding_pr
from agentit.portal.store import AssessmentStore
from agentit.runner import run_assessment

log = logging.getLogger(__name__)

OPERATION_TIMEOUT = 300  # 5 minutes max for any blocking operation


async def _with_timeout(coro, timeout: int = OPERATION_TIMEOUT):
    """Wrap an async operation with a timeout to prevent stuck requests."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"Operation timed out after {timeout}s")

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="AgentIT Portal")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _safe_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http", ""):
        return "#"
    return value


_DIMENSION_LABELS: dict[str, str] = {
    "ha_dr": "HA/DR",
    "cicd": "CI/CD",
    "data_governance": "Data Governance",
}


def _format_dimension(value: str) -> str:
    """Format dimension names for display. Uses explicit mapping for acronyms,
    falls back to replacing underscores and title-casing."""
    if value in _DIMENSION_LABELS:
        return _DIMENSION_LABELS[value]
    return value.replace("_", " ").title()


templates.env.filters["safe_url"] = _safe_url
templates.env.filters["dimension_label"] = _format_dimension


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": 404, "detail": "Page not found"},
        status_code=404,
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    log.exception("Internal server error on %s", request.url.path)
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": 500, "detail": "Internal server error"},
        status_code=500,
    )


store = AssessmentStore()


def get_store() -> AssessmentStore:
    return store


def _enrich_fleet_with_cluster_status(fleet: list[dict], _store: object | None = None) -> list[dict]:
    """Check cluster for each app's deployment status."""
    import json as _json

    # Get all Argo CD apps in one call
    argo_status: dict[str, dict] = {}
    raw = _run_cmd(["oc", "get", "applications.argoproj.io", "-n", "openshift-gitops", "-o", "json"])
    if raw:
        try:
            for a in _json.loads(raw).get("items", []):
                name = a.get("metadata", {}).get("name", "")
                dest = a.get("spec", {}).get("destination", {})
                cluster = dest.get("server", "unknown")
                namespace = dest.get("namespace", "default")
                argo_status[name] = {
                    "sync": a.get("status", {}).get("sync", {}).get("status", "Unknown"),
                    "health": a.get("status", {}).get("health", {}).get("status", "Unknown"),
                    "cluster": cluster,
                    "namespace": namespace,
                }
        except Exception:
            log.debug("Failed to fetch Argo CD apps for fleet enrichment", exc_info=True)

    for app in fleet:
        app_name = app["repo_name"].lower().replace("_", "-").replace(".", "-")
        argo = argo_status.get(app_name)
        apply_results = None
        try:
            apply_results = _store.get_apply_results(app["id"]) if _store else None
        except Exception:
            log.debug("Failed to get apply results for %s", app["id"], exc_info=True)

        if argo:
            app["deploy_status"] = "synced" if argo["sync"] == "Synced" else "out-of-sync"
            app["deploy_health"] = argo["health"].lower()
            app["deploy_cluster"] = argo["cluster"]
            app["deploy_namespace"] = argo["namespace"]
        elif apply_results and apply_results.get("applied"):
            app["deploy_status"] = "applied"
            app["deploy_health"] = "unknown"
            app["deploy_cluster"] = "local"
            app["deploy_namespace"] = apply_results.get("namespace", "default")
        else:
            app["deploy_status"] = "not deployed"
            app["deploy_health"] = "—"
            app["deploy_cluster"] = "—"
            app["deploy_namespace"] = "—"

    return fleet


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    s = get_store()
    fleet = s.get_fleet_data()
    fleet = await asyncio.to_thread(_enrich_fleet_with_cluster_status, fleet, s)
    total_apps = len(fleet)
    if total_apps == 0:
        return templates.TemplateResponse(request, "dashboard.html", {
            "assessments": [], "total_apps": 0, "avg_score": 0, "critical_total": 0, "trends": {},
        })
    avg_score = sum(r["latest_score"] for r in fleet) / total_apps
    critical_total = sum(r["critical_count"] for r in fleet)
    return templates.TemplateResponse(
        request,
        "fleet.html",
        {
            "fleet": fleet,
            "total_apps": total_apps,
            "avg_score": avg_score,
            "critical_total": critical_total,
        },
    )


@app.get("/fleet", response_class=HTMLResponse)
async def fleet_redirect() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=301)


@app.get("/api/fleet")
async def api_fleet() -> JSONResponse:
    return JSONResponse(get_store().get_fleet_data())


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request) -> HTMLResponse:
    events = get_store().list_events(limit=100)
    return templates.TemplateResponse(request, "events.html", {"events": events})


@app.post("/api/webhook/assess")
async def webhook_assess(request: Request):
    """Trigger an assessment via webhook. Accepts JSON body: {repo_url, criticality}"""
    body = await request.json()
    repo_url = body.get("repo_url")
    if not repo_url:
        raise HTTPException(400, "repo_url required")
    criticality = body.get("criticality", "medium")
    report = await _with_timeout(asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality))
    assessment_id = get_store().save(report)
    return JSONResponse({"assessment_id": assessment_id, "overall_score": report.overall_score})


@app.post("/api/webhook/onboard")
async def webhook_onboard(request: Request):
    """Trigger onboarding via webhook (called by Argo Events Sensor for low-score assessments)."""
    body = await request.json()
    log.info("webhook_onboard received event: %s", body.get("eventId", "unknown"))

    # Extract assessment_id: correlationId first, then result.details.assessment_id
    assessment_id = body.get("correlationId")
    if not assessment_id:
        result = body.get("result") or {}
        details = result.get("details") or {}
        assessment_id = details.get("assessment_id")
    if not assessment_id:
        raise HTTPException(400, "assessment_id not found in event body")

    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(404, f"Assessment {assessment_id} not found")

    try:
        files, orch_summary = await _with_timeout(asyncio.to_thread(_run_onboarding, report, assessment_id))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Onboarding failed for assessment %s", assessment_id)
        return JSONResponse(
            {"error": str(exc), "assessment_id": assessment_id},
            status_code=500,
        )

    s.save_onboarding(assessment_id, files, orchestration=orch_summary)

    # Trigger image build
    from agentit.image_builder import build_app_image
    build_result = await asyncio.to_thread(build_app_image, report.repo_url, report.repo_name)
    build_status = build_result.get("image_ref", build_result.get("error", "unknown"))
    if "error" not in build_result:
        s.log_event("image-builder", "build-triggered", report.repo_name, "info",
                    f"Building image: {build_result['image_ref']}")

    log.info("webhook_onboard completed for %s: %d files, build=%s", assessment_id, len(files), build_status)
    return JSONResponse({
        "assessment_id": assessment_id,
        "repo_url": report.repo_url,
        "files_generated": len(files),
        "categories": list({f["category"] for f in files}),
        "image_build": build_status,
    })


@app.get("/api/events")
async def api_events(limit: int = 50, target_app: str | None = None):
    return JSONResponse(get_store().list_events(limit=limit, target_app=target_app))


@app.get("/assess", response_class=HTMLResponse)
async def assess_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "assess_form.html")


def _get_llm_client():
    import os
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        try:
            from agentit.llm import LLMClient
            return LLMClient()
        except Exception as exc:
            log.warning("LLM client init failed (continuing without): %s", exc)
    return None


def _clone_assess_cleanup(repo_url: str, criticality: str, infra_repo_url: str | None = None):
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=_get_llm_client(), infra_repo_url=infra_repo_url,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


@app.post("/assess", response_model=None)
async def assess_submit(
    request: Request,
    repo_url: str = Form(...),
    criticality: str = Form("medium"),
    infra_repo_url: str = Form(""),
):
    infra = infra_repo_url.strip() or None

    # Auto-create infra repo if not provided
    if not infra:
        infra = await asyncio.to_thread(_auto_create_infra_repo, repo_url)

    try:
        report = await asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality, infra)
    except Exception as exc:
        log.exception("Assessment failed for %s", repo_url)
        return templates.TemplateResponse(
            request, "assess_form.html", {"error": str(exc)}, status_code=400,
        )
    assessment_id = get_store().save(report)
    return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)


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


@app.post("/self-assess", response_model=None)
async def self_assess_route(request: Request):
    """One-click self-assessment — AgentIT assesses its own repo."""
    repo_url = "https://github.com/alimobrem/AgentIT"
    infra = await asyncio.to_thread(_auto_create_infra_repo, repo_url)
    try:
        report = await _with_timeout(
            asyncio.to_thread(_clone_assess_cleanup, repo_url, "high", infra)
        )
    except Exception as exc:
        log.exception("Self-assessment failed")
        return RedirectResponse(url=f"/?error={quote(str(exc)[:200])}", status_code=303)
    assessment_id = get_store().save(report)
    get_store().log_event("self-assess", "assessment-complete", "agentit", "info",
                          f"Self-assessment complete: {report.overall_score:.0f}/100")
    return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)


@app.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def assessment_detail(request: Request, assessment_id: str) -> HTMLResponse:
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    scores_sorted = sorted(report.scores, key=lambda s: s.score)
    urgent_findings = [
        f
        for s in report.scores
        for f in s.findings
        if f.severity in (Severity.critical, Severity.high)
    ]

    remediations = s.list_remediations(assessment_id)
    slos = s.list_slos(assessment_id)
    onboardings = s.list_onboardings(assessment_id)

    return templates.TemplateResponse(
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
        },
    )


@app.get("/api/assessments")
async def api_list() -> JSONResponse:
    return JSONResponse(get_store().list_all())


@app.get("/api/assessments/{assessment_id}")
async def api_detail(assessment_id: str) -> JSONResponse:
    report = get_store().get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return JSONResponse(report.model_dump(mode="json"))


@app.post("/assessments/{assessment_id}/delete", response_model=None)
async def delete_assessment(assessment_id: str):
    s = get_store()
    if not s.delete(assessment_id):
        raise HTTPException(404, "Assessment not found")
    s.log_event("portal", "assessment-deleted", None, "info", f"Deleted assessment {assessment_id}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/assessments/{assessment_id}/slos/{slo_id}/delete", response_model=None)
async def delete_slo(assessment_id: str, slo_id: str):
    s = get_store()
    s._conn.execute("DELETE FROM slos WHERE id = ? AND assessment_id = ?", (slo_id, assessment_id))
    s._conn.commit()
    return RedirectResponse(url=f"/assessments/{assessment_id}/slos", status_code=303)


@app.post("/assessments/{assessment_id}/remediations/{rem_id}/delete", response_model=None)
async def delete_remediation(assessment_id: str, rem_id: str):
    s = get_store()
    s._conn.execute("DELETE FROM remediations WHERE id = ? AND assessment_id = ?", (rem_id, assessment_id))
    s._conn.commit()
    return RedirectResponse(url=f"/assessments/{assessment_id}/remediations", status_code=303)


@app.post("/gates/{gate_id}/cancel", response_model=None)
async def cancel_gate(gate_id: str):
    s = get_store()
    s.resolve_gate(gate_id, "cancelled", "portal-user")
    return RedirectResponse(url="/gates", status_code=303)


def _run_onboarding(
    report: AssessmentReport, assessment_id: str | None = None,
) -> tuple[list[dict], dict]:
    """Run orchestrated onboarding. Returns (files, orchestration_summary)."""
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
                    all_files.append(
                        {
                            "category": ar.category,
                            "path": rel_path,
                            "description": rel_path,
                            "content": file_path.read_text(encoding="utf-8"),
                        }
                    )

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


@app.get("/assessments/{assessment_id}/onboarding-history", response_class=HTMLResponse)
async def onboarding_history(request: Request, assessment_id: str) -> HTMLResponse:
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    onboardings = s.list_onboardings(assessment_id)
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
    return templates.TemplateResponse(request, "onboarding_history.html", {
        "report": report,
        "onboardings": onboardings,
        "assessment_id": assessment_id,
    })


@app.post("/assessments/{assessment_id}/onboard", response_model=None)
async def onboard_submit(assessment_id: str):
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    files, orch_summary = await asyncio.to_thread(_run_onboarding, report, assessment_id)
    s.save_onboarding(assessment_id, files, orchestration=orch_summary)

    # Trigger image build for the app
    from agentit.image_builder import build_app_image
    build_result = await asyncio.to_thread(build_app_image, report.repo_url, report.repo_name)
    if "error" in build_result:
        log.warning("Image build trigger failed for %s: %s", report.repo_name, build_result["error"])
    else:
        log.info("Image build triggered: %s → %s", report.repo_name, build_result.get("image_ref"))
        s.log_event("image-builder", "build-triggered", report.repo_name, "info",
                    f"Building image: {build_result.get('image_ref')}")

    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results", status_code=303,
    )


@app.get("/assessments/{assessment_id}/onboard-results", response_class=HTMLResponse)
async def onboard_results(request: Request, assessment_id: str) -> HTMLResponse:
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    grouped: dict[str, list[dict]] = {}
    for f in files:
        grouped.setdefault(f["category"], []).append(f)

    orchestration = s.get_orchestration(assessment_id) or {}
    apply_results = s.get_apply_results(assessment_id)

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
    onboardings = s.list_onboardings(assessment_id)
    pr_url = onboardings[0]["pr_url"] if onboardings and onboardings[0]["pr_url"] else ""
    if pr_url:
        from agentit.portal.github_pr import get_pr_status
        pr_status = await asyncio.to_thread(get_pr_status, pr_url)

    return templates.TemplateResponse(
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


@app.get("/api/assessments/{assessment_id}/manifests")
async def api_manifests(assessment_id: str) -> JSONResponse:
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    return JSONResponse(files)


@app.get("/api/assessments/{assessment_id}/manifests/download")
async def download_manifests(assessment_id: str):
    """Download all onboarding manifests as a zip file."""
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = s.get_onboarding(assessment_id)
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


@app.post("/assessments/{assessment_id}/apply", response_model=None)
async def apply_to_cluster(request: Request, assessment_id: str):
    """Apply onboarding manifests to the current cluster."""
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    files = s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    form = await request.form()
    namespace = str(form.get("namespace", "default"))
    dry_run = form.get("dry_run") == "true"

    results = await asyncio.to_thread(
        apply_manifests_to_cluster, files, namespace, dry_run,
    )

    s.save_apply_results(assessment_id, results, namespace, dry_run)

    applied = len(results["applied"])
    skipped = len(results["skipped"])
    errs = len(results["errors"])
    return RedirectResponse(
        url=(
            f"/assessments/{assessment_id}/onboard-results"
            f"?applied={applied}&skipped={skipped}&errors={errs}"
            f"&dry_run={'true' if dry_run else 'false'}"
        ),
        status_code=303,
    )


@app.post("/api/install-operator", response_model=None)
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
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?operator_installed={package}&install_status={result['status']}",
            status_code=303,
        )
    return JSONResponse(result)


@app.get("/gates", response_class=HTMLResponse)
async def gates_page(request: Request):
    """Show pending approval gates. Auto-expires gates older than 24h."""
    s = get_store()
    expired_count = s.expire_stale_gates(hours=24)
    if expired_count:
        s.log_event("portal", "gates-expired", None, "info",
                    f"Auto-expired {expired_count} stale gate(s)")

    all_gates = s.list_all_gates()
    pending = [g for g in all_gates if g["status"] == "pending"]
    stale = s.get_stale_gates(hours=4)
    stale_ids = {g["id"] for g in stale}
    for g in pending:
        g["stale"] = g["id"] in stale_ids
    resolved = [g for g in all_gates if g["status"] in ("approved", "rejected", "expired")]
    resolved.sort(key=lambda g: g.get("resolved_at") or g.get("created_at", ""), reverse=True)
    return templates.TemplateResponse(request, "gates.html", {
        "pending": pending, "resolved": resolved[:20],
        "stale_count": len(stale), "expired_count": expired_count,
    })


@app.post("/gates/{gate_id}/resolve", response_model=None)
async def resolve_gate(request: Request, gate_id: str):
    form = await request.form()
    status = form.get("status")
    resolved_by = form.get("resolved_by", "portal-user")
    s = get_store()

    gates = s.list_gates(status="pending")
    gate = next((g for g in gates if g["id"] == gate_id), None)
    if gate is None:
        raise HTTPException(404, "Gate not found")

    s.resolve_gate(gate_id, status, resolved_by)

    if status == "approved" and gate.get("assessment_id"):
        assessment_id = gate["assessment_id"]
        files = s.get_onboarding(assessment_id)
        report = s.get(assessment_id)
        if files and report:
            namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
            results = await asyncio.to_thread(
                apply_manifests_to_cluster, files, namespace, False,
            )
            s.save_apply_results(assessment_id, results, namespace, False)
            applied = len(results["applied"])
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?applied={applied}&gate_approved=true",
                status_code=303,
            )

    return RedirectResponse(url="/gates", status_code=303)


@app.get("/api/gates")
async def api_gates(status: str = "pending"):
    return JSONResponse(get_store().list_gates(status=status))


@app.post("/assessments/{assessment_id}/create-pr", response_model=None)
async def create_pr(assessment_id: str):
    """Commit manifests to GitOps infra repo (or app repo as fallback)."""
    from agentit.portal.github_pr import commit_to_infra_repo, ensure_applicationset

    s = get_store()
    report = s.get(assessment_id)
    files = s.get_onboarding(assessment_id)
    if report is None or files is None:
        raise HTTPException(status_code=404, detail="Assessment or onboarding not found")

    try:
        if report.infra_repo_url:
            result = await asyncio.to_thread(
                commit_to_infra_repo, report.infra_repo_url, report.repo_name, files,
            )
            await asyncio.to_thread(ensure_applicationset, report.infra_repo_url)
        else:
            result = await asyncio.to_thread(
                create_onboarding_pr, report.repo_url, report.repo_name, files,
            )
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
    s.update_pr_url(assessment_id, result["pr_url"])
    s.log_event("portal", "pr-created", report.repo_name,
                "info", f"PR created: {result['pr_url']}")
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?pr_url={result['pr_url']}",
        status_code=303,
    )


@app.post("/assessments/{assessment_id}/create-agent-prs", response_model=None)
async def create_agent_prs_route(assessment_id: str):
    """Create per-agent branches and PRs."""
    from agentit.portal.github_pr import create_agent_prs

    s = get_store()
    report = s.get(assessment_id)
    files = s.get_onboarding(assessment_id)
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
        s.update_pr_url(assessment_id, successful[0]["pr_url"])
        s.log_event("orchestrator", "agent-prs-created", report.repo_name,
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


# ── System Health ──────────────────────────────────────────────────────


def _get_watcher_deploy_status() -> dict[str, str]:
    import json as _json
    raw = _run_cmd(["oc", "get", "deployments", "-n", "agentit",
                    "-l", "app.kubernetes.io/instance=agentit", "-o", "json"])
    result: dict[str, str] = {}
    if not raw:
        return result
    try:
        for dep in _json.loads(raw).get("items", []):
            name = dep.get("metadata", {}).get("name", "")
            ready = dep.get("status", {}).get("readyReplicas", 0)
            result[name] = "running" if ready and ready > 0 else "not running"
    except Exception:
        log.debug("Failed to parse deployment status JSON", exc_info=True)
    return result


def _run_cmd(cmd: list[str], timeout: int = 10) -> str | None:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _get_cluster_health() -> dict:
    import json as _json

    result: dict = {
        "argo_apps": [], "argo_synced": False,
        "pods": [], "pods_running": 0, "pods_failed": 0,
        "pipelines": [], "pipeline_status": "Unknown",
        "kafka_ready": False, "publisher_ok": False,
    }

    # Argo CD apps — only show AgentIT-managed apps
    managed_names = {"agentit"}
    try:
        _s = get_store()
        for app_data in _s.get_fleet_data():
            managed_names.add(app_data["repo_name"].lower().replace("_", "-").replace(".", "-"))
    except Exception:
        log.debug("Failed to load fleet for health check", exc_info=True)

    raw = _run_cmd(["oc", "get", "applications.argoproj.io", "-n", "openshift-gitops", "-o", "json"])
    if raw:
        try:
            apps = _json.loads(raw).get("items", [])
            for a in apps:
                name = a.get("metadata", {}).get("name", "?")
                bare_name = name.removeprefix("managed-")
                if name not in managed_names and bare_name not in managed_names:
                    continue
                sync = a.get("status", {}).get("sync", {}).get("status", "Unknown")
                health = a.get("status", {}).get("health", {}).get("status", "Unknown")
                result["argo_apps"].append({"name": name, "sync": sync, "health": health})
            result["argo_synced"] = all(a["sync"] == "Synced" for a in result["argo_apps"]) if result["argo_apps"] else True
        except Exception:
            log.warning("Failed to parse Argo CD apps JSON", exc_info=True)

    # Pods — only show Running/Pending/Failed, skip Completed pipeline pods
    raw = _run_cmd(["oc", "get", "pods", "-n", "agentit", "-o", "json"])
    if raw:
        try:
            pods = _json.loads(raw).get("items", [])
            for p in pods:
                name = p.get("metadata", {}).get("name", "?")
                phase = p.get("status", {}).get("phase", "Unknown")
                if phase in ("Succeeded", "Completed"):
                    continue
                restarts = sum(
                    cs.get("restartCount", 0)
                    for cs in p.get("status", {}).get("containerStatuses", [])
                )
                created = p.get("metadata", {}).get("creationTimestamp", "")
                result["pods"].append({
                    "name": name, "status": phase,
                    "restarts": restarts, "age": created[:16],
                })
            result["pods_running"] = sum(1 for p in result["pods"] if p["status"] == "Running")
            result["pods_failed"] = sum(1 for p in result["pods"] if p["status"] in ("Failed", "Error", "CrashLoopBackOff"))
        except Exception:
            log.warning("Failed to parse pods JSON", exc_info=True)

    # Pipeline runs
    raw = _run_cmd(["oc", "get", "pipelineruns", "-n", "agentit",
                    "-l", "tekton.dev/pipeline=agentit-ci", "-o", "json"], 5)
    if raw:
        try:
            all_runs = _json.loads(raw).get("items", [])
            runs = all_runs[-5:]
            for r in runs:
                name = r.get("metadata", {}).get("name", "?")
                conditions = r.get("status", {}).get("conditions", [{}])
                status = conditions[0].get("reason", "Unknown") if conditions else "Unknown"
                start = r.get("status", {}).get("startTime", "")
                completion = r.get("status", {}).get("completionTime", "")
                duration = ""
                if start and completion:
                    duration = f"{start[:16]} → {completion[:16]}"
                elif start:
                    duration = f"Started {start[:16]}"
                result["pipelines"].append({"name": name, "status": status, "duration": duration})
            if result["pipelines"]:
                result["pipeline_status"] = result["pipelines"][-1]["status"]
            for r in reversed(all_runs):
                conds = r.get("status", {}).get("conditions", [{}])
                if conds and conds[0].get("reason") == "Succeeded":
                    ct = r.get("status", {}).get("completionTime", "")
                    result["last_successful_ci"] = ct[:19] if ct else "?"
                    break
        except Exception:
            log.warning("Failed to parse pipeline runs JSON", exc_info=True)

    # Rollout status
    raw = _run_cmd(["oc", "get", "rollouts.argoproj.io", "agentit", "-n", "agentit", "-o", "json"])
    if raw:
        try:
            ro = _json.loads(raw)
            result["rollout_phase"] = ro.get("status", {}).get("phase", "Unknown")
            result["rollout_step"] = ro.get("status", {}).get("currentStepIndex", 0)
            result["rollout_total_steps"] = len(ro.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", []))
            result["rollout_revision"] = ro.get("status", {}).get("observedGeneration", "?")
        except Exception:
            log.debug("Failed to parse rollout JSON", exc_info=True)

    # Current commit (from the running pod's image labels or git)
    raw = _run_cmd(["oc", "get", "applications.argoproj.io", "agentit", "-n", "openshift-gitops",
                    "-o", "jsonpath={.status.sync.revision}"])
    result["current_commit"] = (raw or "").strip()[:12]

    # Kafka
    raw = _run_cmd(["oc", "get", "kafka", "-n", "agentit", "-o",
                    "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}"])
    result["kafka_ready"] = raw is not None and "True" in raw

    # Event publisher
    try:
        from agentit.events import get_publisher
        pub = get_publisher()
        result["publisher_ok"] = pub._producer is not None
    except Exception:
        log.debug("Failed to check event publisher", exc_info=True)

    return result


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> HTMLResponse:
    data = await asyncio.to_thread(_get_cluster_health)
    return templates.TemplateResponse(request, "health.html", data)


@app.get("/health/pods/{pod_name}", response_class=HTMLResponse)
async def pod_detail_page(request: Request, pod_name: str) -> HTMLResponse:
    import json as _json

    raw = await asyncio.to_thread(_run_cmd, ["oc", "get", "pod", pod_name, "-n", "agentit", "-o", "json"])
    if not raw:
        raise HTTPException(404, f"Pod {pod_name} not found")

    pod = _json.loads(raw)
    status = pod.get("status", {}).get("phase", "Unknown")
    created = pod.get("metadata", {}).get("creationTimestamp", "")[:19]
    containers = []
    total_restarts = 0
    for cs in pod.get("status", {}).get("containerStatuses", []):
        restarts = cs.get("restartCount", 0)
        total_restarts += restarts
        containers.append({
            "name": cs.get("name", "?"),
            "image": cs.get("image", "?"),
            "ready": cs.get("ready", False),
            "restarts": restarts,
        })

    logs = await asyncio.to_thread(
        _run_cmd, ["oc", "logs", pod_name, "-n", "agentit", "--tail=100"], 15,
    ) or "No logs available"

    events_raw = await asyncio.to_thread(
        _run_cmd, ["oc", "get", "events", "-n", "agentit", "--field-selector",
                   f"involvedObject.name={pod_name}", "-o", "json"],
    )
    events = []
    if events_raw:
        try:
            for e in _json.loads(events_raw).get("items", []):
                events.append({
                    "time": e.get("lastTimestamp", "")[:19],
                    "type": e.get("type", "Normal"),
                    "reason": e.get("reason", "?"),
                    "message": e.get("message", "")[:200],
                })
        except Exception:
            log.debug("Failed to parse pod events", exc_info=True)

    return templates.TemplateResponse(request, "pod_detail.html", {
        "pod_name": pod_name,
        "status": status,
        "restarts": total_restarts,
        "created": created,
        "containers": containers,
        "logs": logs,
        "events": events,
    })


@app.get("/health/pipelines/{pipeline_name}", response_class=HTMLResponse)
async def pipeline_detail_page(request: Request, pipeline_name: str) -> HTMLResponse:
    import json as _json

    raw = await asyncio.to_thread(
        _run_cmd, ["oc", "get", "pipelinerun", pipeline_name, "-n", "agentit", "-o", "json"],
    )
    if not raw:
        raise HTTPException(404, f"PipelineRun {pipeline_name} not found")

    pr = _json.loads(raw)
    conditions = pr.get("status", {}).get("conditions", [{}])
    status = conditions[0].get("reason", "Unknown") if conditions else "Unknown"
    start_time = pr.get("status", {}).get("startTime", "")[:19]
    completion_time = (pr.get("status", {}).get("completionTime") or "")[:19]

    tasks = []
    for child in pr.get("status", {}).get("childReferences", []):
        task_name = child.get("pipelineTaskName", child.get("name", "?"))
        conds = child.get("conditions", [])
        task_status = "Unknown"
        if conds:
            task_status = conds[0].get("reason", "Unknown")
        elif child.get("status"):
            task_status = child["status"]
        pod = child.get("name", "")
        tasks.append({"name": task_name, "status": task_status, "pod": pod})

    # Get logs from the last task pod
    logs = ""
    if tasks:
        last_pod = tasks[-1].get("pod", "")
        if last_pod:
            logs = await asyncio.to_thread(
                _run_cmd, ["oc", "logs", last_pod, "-n", "agentit", "--tail=50", "--all-containers"], 15,
            ) or ""

    return templates.TemplateResponse(request, "pipeline_detail.html", {
        "pipeline_name": pipeline_name,
        "status": status,
        "start_time": start_time,
        "completion_time": completion_time,
        "tasks": tasks,
        "logs": logs,
    })


@app.get("/api/health")
async def api_health():
    data = await asyncio.to_thread(_get_cluster_health)
    return JSONResponse({
        "argo_synced": data["argo_synced"],
        "pods_running": data["pods_running"],
        "pipeline_status": data["pipeline_status"],
        "kafka_ready": data["kafka_ready"],
    })


# ── Schedules ──────────────────────────────────────────────────────────


_CRON_HUMAN = {
    "0 3 1 * *": "Monthly (1st, 3am UTC)",
    "0 4 * * 1": "Weekly (Mon 4am UTC)",
    "0 5 * * 1": "Weekly (Mon 5am UTC)",
    "0 2 * * 3": "Weekly (Wed 2am UTC)",
    "0 6 * * 1": "Weekly (Mon 6am UTC)",
}

_SCHEDULE_FILES = {
    "compliance-cronworkflow.yaml": ("compliance", "Compliance re-assessment"),
    "cost-cronworkflow.yaml": ("cost", "Cost optimization report"),
    "dependency-cronworkflow.yaml": ("dependency", "Dependency scan"),
    "chaos-schedule.yaml": ("chaos", "Chaos experiments"),
}

_WATCHER_AGENTS = [
    {"name": "vuln-watcher", "mode": "Kafka consumer + polling", "interval": "6 hours"},
    {"name": "slo-tracker", "mode": "Polling", "interval": "5 minutes"},
    {"name": "drift-detector", "mode": "Argo CD polling", "interval": "10 minutes"},
]


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request) -> HTMLResponse:
    import yaml as _yaml

    s = get_store()
    fleet = s.get_fleet_data()
    schedules: list[dict] = []

    for app_data in fleet:
        aid = app_data["id"]
        files = s.get_onboarding(aid)
        if not files:
            continue
        for f in files:
            sched_info = _SCHEDULE_FILES.get(f["path"])
            if sched_info is None:
                continue
            agent, desc = sched_info
            try:
                doc = _yaml.safe_load(f["content"])
                cron = doc.get("spec", {}).get("schedule", "unknown")
                concurrency = doc.get("spec", {}).get("concurrencyPolicy", "Allow")
            except (ValueError, AttributeError):
                cron = "unknown"
                concurrency = "unknown"
            override_key = f"schedule:{app_data['repo_name']}:{agent}"
            override = s.get_setting(override_key)
            if override:
                cron = override
            enabled_key = f"schedule:{app_data['repo_name']}:{agent}:enabled"
            enabled_val = s.get_setting(enabled_key)
            enabled = enabled_val != "false"

            schedules.append({
                "app_name": app_data["repo_name"],
                "job_name": desc,
                "schedule": cron,
                "human_schedule": _CRON_HUMAN.get(cron, cron),
                "agent": agent,
                "concurrency": concurrency,
                "enabled": enabled,
            })

    agents = s.list_agents()
    deploy_status = await asyncio.to_thread(_get_watcher_deploy_status)
    watchers = []
    for w in _WATCHER_AGENTS:
        agent_record = next((a for a in agents if a["agent_name"] == w["name"]), None)
        deploy_name = f"agentit-{w['name']}"
        if agent_record:
            status = agent_record["status"]
        elif deploy_status.get(deploy_name) == "running":
            status = "active"
        else:
            status = "not deployed"
        watchers.append({**w, "status": status})

    return templates.TemplateResponse(request, "schedules.html", {
        "schedules": schedules,
        "watchers": watchers,
        "apps": fleet,
    })


@app.post("/schedules/update", response_model=None)
async def update_schedule(request: Request):
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    job_key = str(form.get("job_key", ""))
    schedule = str(form.get("schedule", "")).strip()
    if app_name and job_key and schedule:
        get_store().set_setting(f"schedule:{app_name}:{job_key}", schedule)
        get_store().log_event(
            "portal", "schedule-updated", app_name, "info",
            f"Schedule for {job_key} updated to: {schedule}",
        )
    return RedirectResponse(url="/schedules", status_code=303)


@app.post("/schedules/toggle", response_model=None)
async def toggle_schedule(request: Request):
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    job_key = str(form.get("job_key", ""))
    enabled = str(form.get("enabled", "true"))
    if app_name and job_key:
        get_store().set_setting(f"schedule:{app_name}:{job_key}:enabled", enabled)
        action = "enabled" if enabled == "true" else "disabled"
        get_store().log_event(
            "portal", f"schedule-{action}", app_name, "info",
            f"Schedule {job_key} {action} for {app_name}",
        )
    return RedirectResponse(url="/schedules", status_code=303)


# ── Agents ─────────────────────────────────────────────────────────────


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    agents = get_store().list_agents()

    # Merge long-lived watcher agents that aren't in the registry
    registered_names = {a["agent_name"] for a in agents}
    for w in _WATCHER_AGENTS:
        if w["name"] not in registered_names:
            agents.append({
                "agent_name": w["name"],
                "category": w["mode"],
                "status": "deployed",
                "capabilities": f"interval: {w['interval']}",
                "registered_at": "—",
                "last_heartbeat": "—",
            })

    active = sum(1 for a in agents if a["status"] == "active")
    last_hb = max((a["last_heartbeat"] or "" for a in agents), default="—")
    return templates.TemplateResponse(request, "agents.html", {
        "agents": agents,
        "total": len(agents),
        "active": active,
        "last_heartbeat": last_hb[:19] if last_hb != "—" else "—",
    })


@app.get("/agents/{agent_name}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_name: str) -> HTMLResponse:
    s = get_store()
    agents = s.list_agents()
    agent = next((a for a in agents if a["agent_name"] == agent_name), None)

    # Long-lived agents may not be in the registry — create a synthetic entry
    if agent is None:
        watcher = next((w for w in _WATCHER_AGENTS if w["name"] == agent_name), None)
        if watcher is not None:
            agent = {
                "agent_name": agent_name,
                "category": watcher["mode"],
                "status": "deployed",
                "capabilities": f"interval: {watcher['interval']}",
                "registered_at": "—",
                "last_heartbeat": "—",
            }
        else:
            raise HTTPException(status_code=404, detail="Agent not found")

    events = s.list_events_by_agent(agent_name, limit=50)
    remediations = s.list_remediations_by_agent(agent_name)
    pending = sum(1 for r in remediations if r["status"] not in ("completed", "applied"))
    completed = sum(1 for r in remediations if r["status"] in ("completed", "applied"))

    return templates.TemplateResponse(request, "agent_detail.html", {
        "agent": agent,
        "events": events,
        "remediations": remediations,
        "pending": pending,
        "completed": completed,
    })


@app.get("/api/agents")
async def api_agents(status: str = "active"):
    return JSONResponse(get_store().list_agents(status=status))


# ── Remediations ───────────────────────────────────────────────────────


@app.get("/assessments/{assessment_id}/remediations", response_class=HTMLResponse)
async def remediations_page(request: Request, assessment_id: str) -> HTMLResponse:
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    remediations = s.list_remediations(assessment_id)
    pending = sum(1 for r in remediations if r["status"] not in ("completed", "applied"))
    completed = sum(1 for r in remediations if r["status"] in ("completed", "applied"))
    return templates.TemplateResponse(request, "remediations.html", {
        "report": report,
        "remediations": remediations,
        "assessment_id": assessment_id,
        "total": len(remediations),
        "pending": pending,
        "completed": completed,
    })


@app.post("/assessments/{assessment_id}/remediations/{rem_id}/complete", response_model=None)
async def complete_remediation(assessment_id: str, rem_id: str):
    s = get_store()
    remediations = s.list_remediations(assessment_id)
    if not any(r["id"] == rem_id for r in remediations):
        raise HTTPException(status_code=404, detail="Remediation not found for this assessment")
    s.complete_remediation(rem_id)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/remediations", status_code=303,
    )


@app.post("/assessments/{assessment_id}/remediations/{rem_id}/status", response_model=None)
async def update_remediation_status(request: Request, assessment_id: str, rem_id: str):
    form = await request.form()
    status = str(form.get("status", ""))
    if status not in ("generated", "applied", "blocked", "completed"):
        raise HTTPException(400, "Invalid status")
    redirect = str(form.get("redirect", ""))
    s = get_store()
    s.update_remediation_status(rem_id, status)
    dest = f"/assessments/{assessment_id}/remediations"
    if redirect.startswith("/agents/"):
        dest = redirect
    return RedirectResponse(url=dest, status_code=303)


@app.get("/api/assessments/{assessment_id}/remediations")
async def api_remediations(assessment_id: str):
    return JSONResponse(get_store().list_remediations(assessment_id))


# ── SLOs ───────────────────────────────────────────────────────────────


@app.get("/assessments/{assessment_id}/slos", response_class=HTMLResponse)
async def slos_page(request: Request, assessment_id: str) -> HTMLResponse:
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    slos = s.list_slos(assessment_id)
    met = sum(1 for sl in slos if sl["status"] == "met")
    breached = sum(1 for sl in slos if sl["status"] == "breached")
    return templates.TemplateResponse(request, "slos.html", {
        "report": report,
        "slos": slos,
        "assessment_id": assessment_id,
        "total": len(slos),
        "met": met,
        "breached": breached,
    })


@app.post("/assessments/{assessment_id}/slos/add", response_model=None)
async def add_slo(request: Request, assessment_id: str):
    s = get_store()
    if s.get(assessment_id) is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    form = await request.form()
    metric_name = str(form.get("metric_name", "")).strip()
    target_str = str(form.get("target_value", "")).strip()
    if not metric_name or not target_str:
        raise HTTPException(status_code=400, detail="metric_name and target_value required")
    s.save_slo(assessment_id, metric_name, float(target_str))
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/slos", status_code=303,
    )


@app.get("/api/assessments/{assessment_id}/slos")
async def api_slos(assessment_id: str):
    return JSONResponse(get_store().list_slos(assessment_id))


# ── Settings + Auto-Mode ───────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    s = get_store()
    auto_mode = s.get_setting("auto_mode") in ("true", "1", "on")
    llm_available = _get_llm_client() is not None
    recent_actions = s.list_events_by_agent("auto-mode", limit=20)
    return templates.TemplateResponse(request, "settings.html", {
        "auto_mode": auto_mode,
        "llm_available": llm_available,
        "recent_actions": recent_actions,
    })


@app.post("/settings/auto-mode", response_model=None)
async def toggle_auto_mode(request: Request):
    form = await request.form()
    value = str(form.get("value", "false")).lower()
    s = get_store()
    s.set_setting("auto_mode", value)
    s.log_event(
        "portal", "auto-mode-toggled", None,
        "info", f"Auto-mode {'enabled' if value == 'true' else 'disabled'}",
    )
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/api/settings")
async def api_settings():
    return JSONResponse(get_store().list_settings())


@app.get("/api/settings/{key}")
async def api_get_setting(key: str):
    val = get_store().get_setting(key)
    if val is None:
        raise HTTPException(404, f"Setting '{key}' not found")
    return JSONResponse({"key": key, "value": val})


@app.post("/api/settings/{key}")
async def api_set_setting(request: Request, key: str):
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "value required")
    get_store().set_setting(key, str(value))
    return JSONResponse({"key": key, "value": str(value)})


@app.post("/api/webhook/auto-apply")
async def webhook_auto_apply(request: Request):
    """Auto-apply manifests if auto-mode is on and LLM classifies as safe."""
    body = await request.json()
    assessment_id = body.get("assessment_id")
    namespace = body.get("namespace", "default")
    if not assessment_id:
        raise HTTPException(400, "assessment_id required")

    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(404, "Assessment not found")

    files = s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(404, "Onboarding not found — run onboarding first")

    orch = s.get_orchestration(assessment_id) or {}
    auto_approve = orch.get("auto_approve", False)

    from agentit.automode import AutoMode
    engine = AutoMode(store=s, publisher=None, llm_client=_get_llm_client())

    result = await asyncio.to_thread(
        engine.execute, assessment_id, files, namespace,
        report.criticality, auto_approve, report.repo_name,
    )

    log.info("auto-apply result for %s: %s — %s", assessment_id, result["action"], result["reason"])
    return JSONResponse(result)


@app.post("/api/webhook/remediate")
async def webhook_remediate(request: Request):
    """Trigger the full remediation loop: assess → onboard → apply → verify.

    Called by watcher agents or external triggers when an issue is detected.
    """
    body = await request.json()
    repo_url = body.get("repo_url")
    if not repo_url:
        raise HTTPException(400, "repo_url required")

    app_name = body.get("app_name", repo_url.rstrip("/").split("/")[-1].removesuffix(".git"))
    criticality = body.get("criticality", "medium")
    reason = body.get("reason", "webhook trigger")

    from agentit.remediation_loop import RemediationLoop
    from agentit.events import get_publisher

    loop = RemediationLoop(store=get_store(), publisher=get_publisher())
    try:
        result = await asyncio.to_thread(
            loop.trigger, repo_url, app_name, criticality, reason,
        )
    except Exception as exc:
        log.exception("Remediation loop failed for %s", app_name)
        return JSONResponse({"outcome": "failed", "error": str(exc)}, status_code=500)
    finally:
        loop.close()

    return JSONResponse(result)
