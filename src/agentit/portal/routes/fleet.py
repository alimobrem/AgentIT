"""Fleet-wide pages: dashboard, fleet SLOs."""
from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates
from agentit.portal.pending_actions import list_unresolved_escalations, list_unresolved_recommendations

log = logging.getLogger(__name__)

router = APIRouter()


# ── Fleet enrichment ─────────────────────────────────────────────────


_argo_cache: dict = {"data": {}, "ts": 0}
_ARGO_CACHE_TTL = 60  # seconds
# `_enrich_fleet_with_cluster_status` runs in a real OS thread (via
# `asyncio.to_thread`, not just an asyncio task), so concurrent callers can
# genuinely interleave the read-check-write below at the bytecode level --
# an `asyncio.Lock` would not help here since it only excludes other
# coroutines on the same event loop thread, not other OS threads.
_argo_cache_lock = threading.Lock()

# Live GitHub PR state (open/merged/closed) for every PR (see
# pr_tracking.py) -- cached fleet-wide, not per-app, so N apps x M PRs on
# one Fleet page load means one batched round of concurrent GitHub calls
# per TTL window, not N*M live calls per request. A longer TTL than Argo's
# cache above: PR merge/close state changes far less often than a live
# sync/health status.
_pr_status_cache: dict = {"data": {}, "ts": 0}
_PR_STATUS_CACHE_TTL = 120  # seconds
_pr_status_cache_lock = threading.Lock()


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
    with _argo_cache_lock:
        if _argo_cache["data"] and (now - _argo_cache["ts"]) < _ARGO_CACHE_TTL:
            argo_status = _argo_cache["data"]
        else:
            argo_status = None

    if argo_status is None:
        argo_status = {}
        try:
            items = kube.list_custom_resources("argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops")
            from agentit.portal.delivery import application_source_repo_url

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
                    "repo_url": application_source_repo_url(a),
                }
        except Exception:
            log.debug("Failed to fetch Argo CD apps for fleet enrichment", exc_info=True)
        with _argo_cache_lock:
            _argo_cache["data"] = argo_status
            _argo_cache["ts"] = now

    from agentit.portal.delivery import gitops_application_name, is_self_managed_application

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
        # Also counts a literal-named Application (e.g. AgentIT's own
        # `register-self-in-fleet` row, deliberately excluded from the
        # apps/*-directory ApplicationSet -- see
        # `delivery.is_self_managed_application()`) when its source repo
        # actually matches this app's own repo.
        app_item["gitops_registered"] = (
            gitops_application_name(app_name) in argo_status
            or is_self_managed_application(
                argo.get("repo_url") if argo else None, app_item.get("repo_url"),
            )
        )

    return fleet


# ── Routes ────────────────────────────────────────────────────────────


async def _attach_pending_actions(fleet: list[dict], s: object) -> None:
    """"Needs action" badge per app (docs/ui-redesign-proposal.md §2) --
    every open, unmerged PR (Merge/Close on the Actions tab -- see
    routes/pr_actions.py) plus any unresolved rollback/escalation
    recommendation (routes/recommendations.py, portal/pending_actions.py).
    Mutates each fleet row in place with ``pending_actions_count``.
    Requires ``_attach_pr_counts()`` to have already run (reads each row's
    own ``open_prs``) so this doesn't duplicate that function's fleet-wide
    GitHub-status batch.
    """
    for app_item in fleet:
        app_item["pending_actions_count"] = app_item.get("open_prs", 0)

    repo_url_by_app_name = {app_item["repo_name"]: app_item["repo_url"] for app_item in fleet}
    try:
        unresolved_rollbacks, unresolved_escalations = await list_unresolved_recommendations(s)
    except Exception:
        log.debug("Failed to fetch unresolved recommendations for fleet 'needs action' badges", exc_info=True)
        unresolved_rollbacks, unresolved_escalations = [], []

    counts_by_repo: dict[str, int] = {}
    for event in unresolved_rollbacks + unresolved_escalations:
        repo_url = repo_url_by_app_name.get(event.get("target_app"))
        if repo_url:
            counts_by_repo[repo_url] = counts_by_repo.get(repo_url, 0) + 1

    for app_item in fleet:
        app_item["pending_actions_count"] += counts_by_repo.get(app_item["repo_url"], 0)


async def _attach_next_action_state(fleet: list[dict], s: object) -> None:
    """Real "what happens next" per app (docs/onboarding-loop-vision-gap-
    analysis.md's Step 8 discussion / Phase 5) -- reuses
    ``delivery.get_next_action_state()``'s priority-ordered check (unresolved
    escalation > bounded auto-retry in flight > pending finding-
    verification > nothing pending) over the exact same Phase 3/4 data
    (``deliveries.target_findings_json``/``finding_resolution``,
    ``get_finding_failure_count()``, unresolved ``finding-escalated``
    events) rather than a new query. One fleet-wide, unscoped
    ``list_unresolved_events()`` call so this doesn't add a second per-app
    query on top of ``_attach_pending_actions()`` just above. Mutates each
    fleet row in place with ``next_action`` (``None`` when nothing pending/
    failing -- Fleet omits the indicator entirely rather than showing a
    fabricated "all clear" for every row).
    """
    from agentit.portal.delivery import NEXT_ACTION_NONE, get_next_action_state
    try:
        unresolved_escalations = await list_unresolved_escalations(s)
    except Exception:
        log.debug("Failed to fetch unresolved escalations for fleet next-action state", exc_info=True)
        unresolved_escalations = []

    for app_item in fleet:
        try:
            state = await get_next_action_state(
                s, app_item["repo_name"], repo_url=app_item["repo_url"], assessment_id=app_item["id"],
                unresolved_escalations=unresolved_escalations,
            )
        except Exception:
            log.debug("Failed to compute next-action state for %s", app_item["repo_name"], exc_info=True)
            state = None
        app_item["next_action"] = state if state and state["state"] != NEXT_ACTION_NONE else None


async def _attach_pr_counts(fleet: list[dict], s: object) -> None:
    """"Total PRs"/"Open PRs" per app -- real DB/GitHub-backed data (see
    pr_tracking.py's module docstring for exactly what's tracked). "Total"
    is a pure DB count, no GitHub call. "Open" additionally needs a live
    GitHub check for every PR (none carry a stored outcome of their own);
    those are batched into ONE round of concurrent GitHub calls across the
    whole fleet (never one call per app) and cached fleet-wide for
    ``_PR_STATUS_CACHE_TTL`` seconds -- a per-app-per-request live check
    would be too slow/rate-limit-prone for a list view at any real fleet
    size. Mutates each fleet row in place with ``total_prs``/``open_prs``.
    """
    import time as _t

    from agentit.portal.pr_tracking import collect_pr_records, resolve_pr_states

    try:
        all_deliveries = await s.list_all_deliveries(limit=5000)
    except Exception:
        log.debug("Failed to fetch deliveries for fleet PR counts", exc_info=True)
        all_deliveries = []
    try:
        all_onboarding_prs = await s.list_all_onboarding_pr_urls() if hasattr(s, "list_all_onboarding_pr_urls") else []
    except Exception:
        log.debug("Failed to fetch onboarding PR URLs for fleet PR counts", exc_info=True)
        all_onboarding_prs = []

    deliveries_by_app: dict[str, list[dict]] = {}
    for d in all_deliveries:
        if d.get("app_name"):
            deliveries_by_app.setdefault(d["app_name"], []).append(d)
    onboardings_by_repo: dict[str, list[dict]] = {}
    for ob in all_onboarding_prs:
        if ob.get("repo_url"):
            onboardings_by_repo.setdefault(ob["repo_url"], []).append(ob)

    records_by_repo: dict[str, list[dict]] = {}
    for app_item in fleet:
        records_by_repo[app_item["repo_url"]] = collect_pr_records(
            deliveries_by_app.get(app_item["repo_name"], []),
            onboardings_by_repo.get(app_item["repo_url"], []),
        )

    now = _t.monotonic()
    with _pr_status_cache_lock:
        cache_fresh = (now - _pr_status_cache["ts"]) < _PR_STATUS_CACHE_TTL
        status_cache = _pr_status_cache["data"] if cache_fresh else {}
        if not cache_fresh:
            _pr_status_cache["data"] = status_cache
            _pr_status_cache["ts"] = now

    # One batched round of concurrent GitHub calls for every not-yet-cached
    # PR across the ENTIRE fleet, not one round per app -- resolve_pr_states()
    # below then finds everything it needs already in status_cache and makes
    # no further GitHub calls of its own.
    unresolved_urls = list({
        r["pr_url"]
        for records in records_by_repo.values()
        for r in records
        if r["known_state"] is None and r["pr_url"] not in status_cache
    })
    if unresolved_urls:
        from agentit.portal.github_pr import get_pr_status
        results = await asyncio.gather(*(asyncio.to_thread(get_pr_status, u) for u in unresolved_urls))
        status_cache.update(dict(zip(unresolved_urls, results)))

    for records in records_by_repo.values():
        await resolve_pr_states(records, status_cache=status_cache)

    for app_item in fleet:
        records = records_by_repo.get(app_item["repo_url"], [])
        app_item["total_prs"] = len(records)
        app_item["open_prs"] = sum(1 for r in records if r["state"] == "open")


@router.get("/")
async def home() -> RedirectResponse:
    """First-run → Fleet guided empty state; otherwise Ledger inbox."""
    s = await get_store()
    fleet = await s.get_fleet_data()
    if not fleet:
        return RedirectResponse(url="/fleet", status_code=302)
    return RedirectResponse(url="/ledger", status_code=302)


@router.get("/gates")
async def gates_page_redirect() -> RedirectResponse:
    """Retired Gates page → Ledger (stale bookmarks)."""
    return RedirectResponse(url="/ledger", status_code=301)


@router.get("/api/gates")
async def api_gates_retired() -> RedirectResponse:
    """Retired JSON gates API → Ledger HTML (callers should use PR APIs)."""
    return RedirectResponse(url="/ledger", status_code=301)


@router.get("/fleet", response_class=HTMLResponse)
async def fleet_page(request: Request) -> HTMLResponse:
    """Portfolio scoreboard: apps, scores, Assess / Scan / Delete.

    PRs waiting for approval are owned by Ledger's "Needs You" section —
    this page only offers a quiet pointer, never an ops-inbox badge column.
    """
    s = await get_store()
    fleet = await s.get_fleet_data()
    loop = asyncio.get_running_loop()
    fleet = await asyncio.to_thread(_enrich_fleet_with_cluster_status, fleet, s, loop)
    await _attach_pr_counts(fleet, s)
    await _attach_pending_actions(fleet, s)
    await _attach_next_action_state(fleet, s)
    total_apps = len(fleet)
    avg_score = (sum(r["latest_score"] for r in fleet) / total_apps) if total_apps else 0
    critical_total = sum(r["critical_count"] for r in fleet)
    # Same PR-status-derived definition as Ledger's own "Waiting for your
    # approval" stat and base.html's nav badge (pr_tracking.py's
    # count_fleet_prs_waiting_for_approval()) -- any PR that's still open
    # and unmerged on GitHub, not just gate-tracked ones (2026-07-19: the
    # old `gate_type == "gitops-pr-pending"` count both missed the
    # "-shared-namespace" gate variant and every source-repo-pr/app-repo-
    # pr/onboarding PR, which never gets a gate at all -- see
    # pr_tracking.py's module docstring). A free sum over the `open_prs`
    # column `_attach_pr_counts()` already computed above, not a second
    # query -- this banner still reflects this exact request's real state,
    # matching every other Fleet stat on this page.
    pending_need_you = sum(app_item.get("open_prs", 0) for app_item in fleet)

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
            "pending_need_you": pending_need_you,
        },
    )


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


@router.get("/api/fleet")
async def api_fleet() -> JSONResponse:
    s = await get_store()
    return JSONResponse(await s.get_fleet_data())
