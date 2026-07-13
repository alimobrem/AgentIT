"""Per-assessment remediation items: list, complete, status, recommendations."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/assessments/{assessment_id}/remediations", response_class=HTMLResponse)
async def remediations_page(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    remediations = await s.list_remediations(assessment_id)
    pending = sum(1 for r in remediations if r["status"] not in ("completed", "applied"))
    completed = sum(1 for r in remediations if r["status"] in ("completed", "applied"))
    return get_templates().TemplateResponse(request, "remediations.html", {
        "report": report,
        "remediations": remediations,
        "assessment_id": assessment_id,
        "total": len(remediations),
        "pending": pending,
        "completed": completed,
    })


@router.post("/assessments/{assessment_id}/remediations/{rem_id}/delete", response_model=None)
async def delete_remediation(assessment_id: str, rem_id: str):
    s = await get_store()
    await s.delete_remediation(rem_id, assessment_id)
    return RedirectResponse(url=f"/assessments/{assessment_id}/remediations", status_code=303)


@router.post("/assessments/{assessment_id}/remediations/{rem_id}/complete", response_model=None)
async def complete_remediation(assessment_id: str, rem_id: str):
    s = await get_store()
    remediations = await s.list_remediations(assessment_id)
    if not any(r["id"] == rem_id for r in remediations):
        raise HTTPException(status_code=404, detail="Remediation not found for this assessment")
    await s.complete_remediation(rem_id)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/remediations", status_code=303,
    )


@router.post("/assessments/{assessment_id}/remediations/{rem_id}/status", response_model=None)
async def update_remediation_status(request: Request, assessment_id: str, rem_id: str):
    form = await request.form()
    status = str(form.get("status", ""))
    if status not in ("generated", "applied", "blocked", "completed"):
        raise HTTPException(400, "Invalid status")
    redirect = str(form.get("redirect", ""))
    s = await get_store()
    await s.update_remediation_status(rem_id, status)
    dest = f"/assessments/{assessment_id}/remediations"
    if redirect.startswith("/agents/"):
        dest = redirect
    return RedirectResponse(url=dest, status_code=303)


@router.get("/api/assessments/{assessment_id}/remediations")
async def api_remediations(assessment_id: str):
    s = await get_store()
    return JSONResponse(await s.list_remediations(assessment_id))


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
