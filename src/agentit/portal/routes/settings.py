"""Settings page: retention/purge, raw settings API."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.audit import audit_log
from agentit.portal.helpers import get_current_user, get_llm_client, get_retention_days, get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    llm_available = get_llm_client() is not None
    retention_days = get_retention_days()
    purge_result = request.query_params.get("purged")
    return get_templates().TemplateResponse(request, "settings.html", {
        "llm_available": llm_available,
        "retention_days": retention_days,
        "purge_result": purge_result,
    })


@router.post("/settings/purge", response_model=None)
async def purge_old_data(request: Request):
    retention = get_retention_days()
    s = await get_store()
    counts = await s.purge_old_data(retention_days=retention)
    total = sum(counts.values())
    audit_log(actor=get_current_user(request), action="purge", resource="store",
              details={"retention_days": retention, "rows_deleted": total, "by_table": counts})
    return RedirectResponse(url=f"/settings?purged={total}", status_code=303)


@router.get("/api/settings")
async def api_settings():
    s = await get_store()
    return JSONResponse(await s.list_settings())


@router.get("/api/export")
async def export_data():
    """Export all data as JSON for backup/migration."""
    s = await get_store()
    return await s.export_all()


@router.get("/api/settings/{key}")
async def api_get_setting(key: str):
    s = await get_store()
    val = await s.get_setting(key)
    if val is None:
        raise HTTPException(404, f"Setting '{key}' not found")
    return JSONResponse({"key": key, "value": val})


@router.post("/api/settings/{key}")
async def api_set_setting(request: Request, key: str):
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "value required")
    s = await get_store()
    await s.set_setting(key, str(value))
    return JSONResponse({"key": key, "value": str(value)})
