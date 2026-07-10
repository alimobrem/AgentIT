from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agentit.agents.cicd import CICDAgent
from agentit.agents.compliance import ComplianceAgent
from agentit.agents.hardening import HardeningAgent
from agentit.agents.observability import ObservabilityAgent
from agentit.cloner import clone_repo
from agentit.models import AssessmentReport, Severity
from agentit.portal.store import AssessmentStore
from agentit.runner import run_assessment

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="AgentIT Portal")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
store = AssessmentStore()


def get_store() -> AssessmentStore:
    return store


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    assessments = get_store().list_all()
    return templates.TemplateResponse(
        request, "dashboard.html", {"assessments": assessments},
    )


@app.get("/assess", response_class=HTMLResponse)
async def assess_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "assess_form.html")


def _clone_assess_cleanup(repo_url: str, criticality: str):
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(repo_path, repo_url, criticality)
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


@app.post("/assess", response_model=None)
async def assess_submit(
    request: Request,
    repo_url: str = Form(...),
    criticality: str = Form("medium"),
):
    try:
        report = await asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality)
    except Exception as exc:
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
    """Run all 4 agents and collect generated files with categories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        agents = [
            ("security", HardeningAgent(report, base / "security")),
            ("observability", ObservabilityAgent(report, base / "observability")),
            ("cicd", CICDAgent(report, base / "cicd")),
            ("compliance", ComplianceAgent(report, base / "compliance")),
        ]

        all_files: list[dict] = []
        for category, agent in agents:
            result = agent.run()
            for gf in result.files:
                all_files.append(
                    {
                        "category": category,
                        "path": gf.path,
                        "description": gf.description,
                        "content": gf.content,
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
