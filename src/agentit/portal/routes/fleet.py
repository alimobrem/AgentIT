"""Fleet-wide pages: dashboard, fleet SLOs, fleet remediations."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


# ── Fleet enrichment ─────────────────────────────────────────────────


_argo_cache: dict = {"data": {}, "ts": 0}
_ARGO_CACHE_TTL = 60  # seconds


def _enrich_fleet_with_cluster_status(fleet: list[dict], _store=None) -> list[dict]:
    """Check cluster for each app's deployment status. Caches Argo CD data for 60s."""
    import time as _t
    from agentit import kube

    now = _t.monotonic()
    if _argo_cache["data"] and (now - _argo_cache["ts"]) < _ARGO_CACHE_TTL:
        argo_status = _argo_cache["data"]
    else:
        argo_status = {}
        try:
            items = kube.list_custom_resources("argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops")
            for a in items:
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
        _argo_cache["data"] = argo_status
        _argo_cache["ts"] = now

    for app_item in fleet:
        app_name = app_item["repo_name"].lower().replace("_", "-").replace(".", "-")
        argo = argo_status.get(app_name)
        apply_results = None
        try:
            apply_results = _store.get_apply_results(app_item["id"]) if _store else None
        except Exception:
            log.debug("Failed to get apply results for %s", app_item["id"], exc_info=True)

        if argo:
            app_item["deploy_status"] = "synced" if argo["sync"] == "Synced" else "out-of-sync"
            app_item["deploy_health"] = argo["health"].lower()
            app_item["deploy_cluster"] = argo["cluster"]
            app_item["deploy_namespace"] = argo["namespace"]
        elif apply_results and apply_results.get("applied"):
            app_item["deploy_status"] = "applied"
            app_item["deploy_health"] = "unknown"
            app_item["deploy_cluster"] = "local"
            app_item["deploy_namespace"] = apply_results.get("namespace", "default")
        else:
            app_item["deploy_status"] = "not deployed"
            app_item["deploy_health"] = "—"
            app_item["deploy_cluster"] = "—"
            app_item["deploy_namespace"] = "—"

    return fleet


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet = await s.get_fleet_data()
    fleet = await asyncio.to_thread(_enrich_fleet_with_cluster_status, fleet, s.raw if hasattr(s, "raw") else s)
    total_apps = len(fleet)
    if total_apps == 0:
        return get_templates().TemplateResponse(request, "dashboard.html", {
            "assessments": [], "total_apps": 0, "avg_score": 0, "critical_total": 0, "trends": {},
        })
    avg_score = sum(r["latest_score"] for r in fleet) / total_apps
    critical_total = sum(r["critical_count"] for r in fleet)

    from agentit.portal.metrics import fleet_size as _fs, fleet_avg_score as _fas
    _fs.set(total_apps)
    _fas.set(avg_score)

    return get_templates().TemplateResponse(
        request,
        "fleet.html",
        {
            "fleet": fleet,
            "total_apps": total_apps,
            "avg_score": avg_score,
            "critical_total": critical_total,
        },
    )


@router.get("/fleet", response_class=HTMLResponse)
async def fleet_redirect() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=301)


@router.get("/fleet/slos", response_class=HTMLResponse)
async def fleet_slos(request: Request) -> HTMLResponse:
    """Fleet-wide SLO view — all SLOs across all apps."""
    s = await get_store()
    fleet = await s.get_fleet_data()
    all_slos = []
    for app_data in fleet:
        slos = await s.list_slos(app_data["id"])
        for slo in slos:
            slo["app_name"] = app_data["repo_name"]
            slo["app_id"] = app_data["id"]
            all_slos.append(slo)
    breached = [sl for sl in all_slos if sl.get("status") == "breached"]
    return get_templates().TemplateResponse(request, "fleet_slos.html", {
        "slos": all_slos, "breached_count": len(breached),
        "total_count": len(all_slos),
    })


@router.get("/fleet/remediations", response_class=HTMLResponse)
async def fleet_remediations(request: Request) -> HTMLResponse:
    """Fleet-wide remediation view — all remediations across all apps."""
    s = await get_store()
    fleet = await s.get_fleet_data()
    all_remediations = []
    for app_data in fleet:
        remeds = await s.list_remediations(app_data["id"])
        for r in remeds:
            r["app_name"] = app_data["repo_name"]
            r["app_id"] = app_data["id"]
            all_remediations.append(r)
    pending = [r for r in all_remediations if r.get("status") != "completed"]
    return get_templates().TemplateResponse(request, "fleet_remediations.html", {
        "remediations": all_remediations, "pending_count": len(pending),
        "total_count": len(all_remediations),
    })


@router.get("/api/fleet")
async def api_fleet() -> JSONResponse:
    s = await get_store()
    return JSONResponse(await s.get_fleet_data())
