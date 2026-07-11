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
from agentit.portal.cluster_apply import apply_manifests_to_cluster, install_operator
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


@app.post("/api/webhook/onboard")
async def webhook_onboard(request: Request):
    """Trigger onboarding via webhook (called by Argo Events Sensor for low-score assessments)."""
    import logging
    log = logging.getLogger("agentit.portal")
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
        files, orch_summary = await asyncio.to_thread(_run_onboarding, report, assessment_id)
    except Exception as exc:
        log.exception("Onboarding failed for assessment %s", assessment_id)
        return JSONResponse(
            {"error": str(exc), "assessment_id": assessment_id},
            status_code=500,
        )

    s.save_onboarding(assessment_id, files, orchestration=orch_summary)
    log.info("webhook_onboard completed for %s: %d files generated", assessment_id, len(files))
    return JSONResponse({
        "assessment_id": assessment_id,
        "repo_url": report.repo_url,
        "files_generated": len(files),
        "categories": list({f["category"] for f in files}),
    })


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
        s.log_event("orchestrator", "agent-prs-created", report.repo_name,
                    "info", f"Created {len(successful)} per-agent PRs: {pr_list}")

    from urllib.parse import quote
    if errors and not successful:
        return RedirectResponse(
            url=f"/assessments/{assessment_id}/onboard-results?error={quote(errors[0].get('error', 'Unknown'))}",
            status_code=303,
        )

    pr_urls = "|".join(f"{r['agent_name']}={r['pr_url']}" for r in successful)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/onboard-results?agent_prs={quote(pr_urls)}",
        status_code=303,
    )


# ── Agents ─────────────────────────────────────────────────────────────


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    agents = get_store().list_agents()
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
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    events = s.list_events_by_agent(agent_name, limit=50)
    remediations = s.list_remediations_by_agent(agent_name)
    pending = sum(1 for r in remediations if r["status"] == "pending")
    completed = sum(1 for r in remediations if r["status"] == "completed")

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
    pending = sum(1 for r in remediations if r["status"] == "pending")
    completed = sum(1 for r in remediations if r["status"] == "completed")
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
