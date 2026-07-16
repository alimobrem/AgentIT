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


def _enrich_fleet_with_cluster_status(fleet: list[dict], _store=None, _loop=None) -> list[dict]:
    """Check cluster for each app's deployment status. Caches Argo CD data for 60s.

    Runs in a worker thread via `asyncio.to_thread` (see `home()` below), so
    `_store`'s coroutine methods need bridging back onto `_loop` (the event
    loop that constructed the store) via `asyncio.run_coroutine_threadsafe`
    -- the same pattern `EventConsumer._persist_dead_letter` uses for the
    identical constraint (an `asyncpg` pool is bound to its creating loop
    and can't be driven from a different thread's loop).
    """
    import asyncio as _asyncio
    import time as _t
    from agentit import kube

    def _bridge(result):
        if not _asyncio.iscoroutine(result):
            return result
        return _asyncio.run_coroutine_threadsafe(result, _loop).result(timeout=30)

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

    from agentit.portal.delivery import gitops_application_name

    for app_item in fleet:
        app_name = app_item["repo_name"].lower().replace("_", "-").replace(".", "-")
        argo = argo_status.get(app_name)
        apply_results = None
        try:
            apply_results = _bridge(_store.get_apply_results(app_item["id"])) if _store else None
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

        # Same "is there a live Argo CD Application for this app" question
        # `delivery.is_gitops_registered()` asks per-app, answered here from
        # the Argo CD list this enrichment pass already fetched once for
        # every app -- avoids an extra live per-row kube call for a signal
        # this loop already has in hand (docs/ui-redesign-proposal.md §4).
        app_item["gitops_registered"] = gitops_application_name(app_name) in argo_status

    return fleet


# ── Routes ────────────────────────────────────────────────────────────


async def _attach_pending_actions(fleet: list[dict], s: object) -> None:
    """"Needs action" badge per app (docs/ui-redesign-proposal.md §2) -- a
    cheap ``GROUP BY repo_url`` count of pending, app-owner-scoped gates
    (``cluster-admin-review`` excluded: that's a different audience's
    concern, counted separately for the Admin Review nav badge). Mutates
    each fleet row in place with ``pending_actions_count``.

    Keyed by ``repo_url`` (not ``assessment_id``): ``list_gates()`` joins
    each gate back to the specific historical assessment it was created
    against, but a fleet row's ``id`` is always the app's LATEST
    assessment_id (``get_fleet_data()``). A gate created against an older
    assessment of the same app would never match the latest assessment_id
    and would silently drop out of this badge count the moment the app is
    re-assessed -- the same orphaned-gate-attribution bug fixed in
    ``store.py``/``store_pg.py``'s ``list_gates_for_assessment()``.
    """
    from agentit.portal.delivery import ADMIN_REVIEW_GATE_TYPE
    try:
        pending_gates = await s.list_gates(status="pending")
    except Exception:
        log.debug("Failed to fetch pending gates for fleet 'needs action' badges", exc_info=True)
        pending_gates = []

    counts: dict[str, int] = {}
    for g in pending_gates:
        if g.get("gate_type") == ADMIN_REVIEW_GATE_TYPE:
            continue
        repo_url = g.get("repo_url")
        if repo_url:
            counts[repo_url] = counts.get(repo_url, 0) + 1

    for app_item in fleet:
        app_item["pending_actions_count"] = counts.get(app_item["repo_url"], 0)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet = await s.get_fleet_data()
    loop = asyncio.get_running_loop()
    fleet = await asyncio.to_thread(_enrich_fleet_with_cluster_status, fleet, s, loop)
    await _attach_pending_actions(fleet, s)
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
