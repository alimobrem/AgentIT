"""Per-assessment SLO definitions and error budgets."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/assessments/{assessment_id}/slos", response_class=HTMLResponse)
async def slos_page(request: Request, assessment_id: str) -> HTMLResponse:
    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    slos = await s.list_slos(assessment_id)
    met = sum(1 for sl in slos if sl["status"] == "met")
    breached = sum(1 for sl in slos if sl["status"] == "breached")
    return get_templates().TemplateResponse(request, "slos.html", {
        "report": report,
        "slos": slos,
        "assessment_id": assessment_id,
        "total": len(slos),
        "met": met,
        "breached": breached,
    })


@router.post("/assessments/{assessment_id}/slos/add", response_model=None)
async def add_slo(request: Request, assessment_id: str):
    s = await get_store()
    if await s.get(assessment_id) is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    form = await request.form()
    metric_name = str(form.get("metric_name", "")).strip()
    target_str = str(form.get("target_value", "")).strip()
    if not metric_name or not target_str:
        raise HTTPException(status_code=400, detail="metric_name and target_value required")
    try:
        target_value = float(target_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="target_value must be a number")
    await s.save_slo(assessment_id, metric_name, target_value)
    return RedirectResponse(
        url=f"/assessments/{assessment_id}/slos", status_code=303,
    )


@router.post("/assessments/{assessment_id}/slos/{slo_id}/delete", response_model=None)
async def delete_slo(assessment_id: str, slo_id: str):
    s = await get_store()
    await s.delete_slo(slo_id, assessment_id)
    return RedirectResponse(url=f"/assessments/{assessment_id}/slos", status_code=303)


@router.get("/api/assessments/{assessment_id}/slos")
async def api_slos(assessment_id: str):
    s = await get_store()
    return JSONResponse(await s.list_slos(assessment_id))
