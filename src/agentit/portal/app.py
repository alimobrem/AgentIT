from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse
from fastapi.templating import Jinja2Templates

from agentit.cloner import clone_repo
from agentit.models import AssessmentReport, Severity
from agentit.portal.cluster_apply import apply_manifests_to_cluster
from agentit.portal.github_pr import create_onboarding_pr
from agentit.portal.store import AssessmentStore
from agentit.runner import run_assessment

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="AgentIT Portal")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _safe_url(value: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http", ""):
        return "#"
    return value


templates.env.filters["safe_url"] = _safe_url
store = AssessmentStore()


def get_store() -> AssessmentStore:
    return store


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    s = get_store()
    assessments = s.list_all()
    fleet = s.get_fleet_data()
    total_apps = len(fleet)
    avg_score = sum(r["latest_score"] for r in fleet) / total_apps if total_apps else 0
    critical_total = sum(r["critical_count"] for r in fleet)
    # Build trend lookup keyed by repo_url
    trends = {r["repo_url"]: r for r in fleet}
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "assessments": assessments,
            "total_apps": total_apps,
            "avg_score": avg_score,
            "critical_total": critical_total,
            "trends": trends,
        },
    )


@app.get("/fleet", response_class=HTMLResponse)
async def fleet_dashboard(request: Request) -> HTMLResponse:
    fleet = get_store().get_fleet_data()
    total_apps = len(fleet)
    avg_score = sum(r["latest_score"] for r in fleet) / total_apps if total_apps else 0
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
    report = await asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality)
    assessment_id = get_store().save(report)
    return JSONResponse({"assessment_id": assessment_id, "overall_score": report.overall_score})


@app.get("/api/events")
async def api_events(limit: int = 50, target_app: str | None = None):
    return JSONResponse(get_store().list_events(limit=limit, target_app=target_app))


@app.get("/assess", response_class=HTMLResponse)
async def assess_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "assess_form.html")


def _get_llm_client():
    import logging
    import os
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        try:
            from agentit.llm import LLMClient
            return LLMClient()
        except Exception as exc:
            logging.getLogger("agentit.portal").warning("LLM client init failed (continuing without): %s", exc)
    return None


def _clone_assess_cleanup(repo_url: str, criticality: str):
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(repo_path, repo_url, criticality, llm_client=_get_llm_client())
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


@app.post("/assess", response_model=None)
async def assess_submit(
    request: Request,
    repo_url: str = Form(...),
    criticality: str = Form("medium"),
):
    import logging
    try:
        report = await asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality)
    except Exception as exc:
        logging.getLogger("agentit.portal").exception("Assessment failed for %s", repo_url)
        return templates.TemplateResponse(
            request, "assess_form.html", {"error": str(exc)}, status_code=400,
        )
    assessment_id = get_store().save(report)
    return RedirectResponse(url=f"/assessments/{assessment_id}", status_code=303)


@app.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def assessment_detail(request: Request, assessment_id: str) -> HTMLResponse:
    report = get_store().get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    scores_sorted = sorted(report.scores, key=lambda s: s.score)
    urgent_findings = [
        f
        for s in report.scores
        for f in s.findings
        if f.severity in (Severity.critical, Severity.high)
    ]

    return templates.TemplateResponse(
        request,
        "assessment_detail.html",
        {
            "report": report,
            "scores_sorted": scores_sorted,
            "urgent_findings": urgent_findings,
            "assessment_id": assessment_id,
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


def _run_onboarding(report: AssessmentReport) -> list[dict]:
    """Run orchestrated onboarding via FleetOrchestrator and collect generated files."""
    from agentit.agents.orchestrator import FleetOrchestrator

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        orch = FleetOrchestrator(report=report, output_dir=base, store=get_store())
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
        return all_files


@app.post("/assessments/{assessment_id}/onboard", response_model=None)
async def onboard_submit(assessment_id: str):
    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")

    files = await asyncio.to_thread(_run_onboarding, report)
    s.save_onboarding(assessment_id, files)
    s.create_gate(assessment_id, "deploy", f"Approve deployment of {report.repo_name} to production (score: {report.overall_score:.0f}/100)")
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

    return templates.TemplateResponse(
        request,
        "onboard_results.html",
        {"report": report, "grouped": grouped, "assessment_id": assessment_id},
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

    applied = len(results["applied"])
    skipped = len(results["skipped"])
    errs = len(results["errors"])
    return RedirectResponse(
        url=(
            f"/assessments/{assessment_id}/onboard-results"
            f"?applied={applied}&skipped={skipped}&errors={errs}"
        ),
        status_code=303,
    )


@app.get("/gates", response_class=HTMLResponse)
async def gates_page(request: Request):
    """Show pending approval gates."""
    pending = get_store().list_gates(status="pending")
    resolved = get_store().list_gates(status="approved") + get_store().list_gates(status="rejected")
    resolved.sort(key=lambda g: g.get("resolved_at", ""), reverse=True)
    return templates.TemplateResponse(request, "gates.html", {
        "pending": pending, "resolved": resolved[:20]
    })


@app.post("/gates/{gate_id}/resolve", response_model=None)
async def resolve_gate(request: Request, gate_id: str):
    form = await request.form()
    status = form.get("status")  # "approved" or "rejected"
    resolved_by = form.get("resolved_by", "portal-user")
    get_store().resolve_gate(gate_id, status, resolved_by)
    return RedirectResponse(url="/gates", status_code=303)


@app.get("/api/gates")
async def api_gates(status: str = "pending"):
    return JSONResponse(get_store().list_gates(status=status))


@app.post("/assessments/{assessment_id}/create-pr", response_model=None)
async def create_pr(assessment_id: str):
    """Create a GitHub PR with the onboarding manifests."""
    s = get_store()
    report = s.get(assessment_id)
    files = s.get_onboarding(assessment_id)
    if report is None or files is None:
        raise HTTPException(status_code=404, detail="Assessment or onboarding not found")
    result = await asyncio.to_thread(
        create_onboarding_pr, report.repo_url, report.repo_name, files,
    )
    if "error" in result:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={result['error']}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?pr_url={result['pr_url']}",
        status_code=303,
    )
