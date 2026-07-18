"""Fleet insights, LLM decision audit, event feed, and the events DLQ."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.portal.delivery import _CICD_SHARED_NAMESPACE_GATE_TYPE, ADMIN_REVIEW_GATE_TYPE
from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()

# Gate types a real code path can still create today: "gitops-pr-pending"/
# the CI/CD-shared-namespace variant of it/"auto-mode-review" (delivery.py/
# automode.py), "rollback-review" (watchers/slo_tracker.py), and the
# "finding-" prefix, which covers both delivery.py's ESCALATION_GATE_TYPE
# ("finding-unresolved-escalation", Phase 4) and routes/webhooks.py's
# per-category dispatcher gates ("finding-{category}"). Direct Apply and its
# "cluster-conflict-review" gate are already gone (routes/gates.py);
# ADMIN_REVIEW_GATE_TYPE ("cluster-admin-review") joined them 2026-07-18 --
# no code path creates it anymore either, but it's kept in this set
# deliberately (unlike the other two retired types, which were simply
# dropped from here): a real, still-pending row of this type existed in
# production the day it was retired, and it still resolves to a genuine
# GitOps PR when approved (see routes/gates.py's generic fallback) -- a
# real, actionable pending item should keep counting here until it's
# actually resolved, not silently stop counting just because its type can
# no longer be freshly created.
_LIVE_GATE_TYPES = {
    ADMIN_REVIEW_GATE_TYPE, "gitops-pr-pending", _CICD_SHARED_NAMESPACE_GATE_TYPE,
    "auto-mode-review", "rollback-review",
}


def _is_live_gate_type(gate_type: str) -> bool:
    return gate_type in _LIVE_GATE_TYPES or gate_type.startswith("finding-")


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet_insights = await s.get_fleet_insights()
    agent_stats = await s.get_agent_stats()
    feedback = await s.get_all_feedback(limit=10)
    low_skills = await s.get_low_effectiveness_skills()
    check_compliance = await s.get_check_compliance()
    loop_health = await s.get_loop_health()

    # get_fleet_insights()'s own `pending_gates` is a blind
    # `COUNT(*) FROM gates WHERE status = 'pending'` -- recompute it here
    # from the real gate list, filtered to gate types that can still
    # actually be created, so a leftover pending row of a since-removed
    # gate type (e.g. a stale `cluster-conflict-review`, per routes/
    # gates.py's own comment on it) can't inflate this fleet-wide stat.
    pending_gates = await s.list_gates(status="pending")
    fleet_insights["pending_gates"] = sum(1 for g in pending_gates if _is_live_gate_type(g.get("gate_type", "")))

    return get_templates().TemplateResponse(request, "insights.html", {
        "insights": fleet_insights,
        "agent_stats": agent_stats,
        "recent_feedback": feedback,
        "low_skills": low_skills,
        "check_compliance": check_compliance,
        "loop_health": loop_health,
    })


@router.get("/decisions", response_class=HTMLResponse)
async def decisions_page(request: Request, decision_type: str = "", attribution: str = "") -> HTMLResponse:
    """Audit every real LLM decision point, attributed by agent/skill.

    Answers "how is agent/skill X actually performing" — how often the LLM
    approves, rejects, or gates its output, and why — by merging the
    fix-review (skill_effectiveness), auto-mode classify, secret-classify,
    and capability-proposal (all three via events) decision records into one
    view. See agentit/llm_decisions.py's module docstring for what each
    decision type covers and how it's attributed.
    """
    from agentit.llm_decisions import list_llm_decisions, summarize_by_attribution

    s = await get_store()
    # `list_llm_decisions` runs in a worker thread (`asyncio.to_thread`), so
    # its store calls need bridging back onto *this* coroutine's event loop
    # -- see llm_decisions.py's `_bridge`.
    loop = asyncio.get_running_loop()
    all_decisions = await asyncio.to_thread(list_llm_decisions, s, 500, loop=loop)
    decision_types = sorted({d["decision_type"] for d in all_decisions})
    attributions = sorted({d["attribution"] for d in all_decisions})

    decisions = all_decisions
    if decision_type:
        decisions = [d for d in decisions if d["decision_type"] == decision_type]
    if attribution:
        decisions = [d for d in decisions if d["attribution"] == attribution]
    summary = summarize_by_attribution(decisions)

    return get_templates().TemplateResponse(request, "decisions.html", {
        "decisions": decisions[:100],
        "summary": summary,
        "decision_type_filter": decision_type,
        "attribution_filter": attribution,
        "decision_types": decision_types,
        "attributions": attributions,
        "total_decisions": len(all_decisions),
    })


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, page: int = 1, per_page: int = 25,
                      q: str = "", severity: str = "", correlation_id: str = "") -> HTMLResponse:
    s = await get_store()
    if correlation_id and hasattr(s, "list_events_by_correlation_id"):
        all_events = await s.list_events_by_correlation_id(correlation_id, limit=2000)
    else:
        all_events = await s.list_events(limit=2000)

    # target_app -> latest assessment id, so the "Target App" column can
    # link to that app's Assessment Detail page wherever a real
    # assessment_id resolves (Fleet, Remediations, and Decisions already
    # link app names the same way; Events showed plain text).
    fleet = await s.get_fleet_data()
    app_ids_by_name = {app_data["repo_name"]: app_data["id"] for app_data in fleet}
    for e in all_events:
        e["assessment_id"] = app_ids_by_name.get(e.get("target_app"))

    if q:
        ql = q.lower()
        all_events = [e for e in all_events
                      if ql in e.get("agent_id", "").lower()
                      or ql in e.get("action", "").lower()
                      or ql in (e.get("target_app") or "").lower()
                      or ql in e.get("summary", "").lower()
                      or ql in (e.get("correlation_id") or "").lower()]
    if severity:
        all_events = [e for e in all_events if e.get("severity") == severity]
    total = len(all_events)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    events = all_events[start:start + per_page]
    return get_templates().TemplateResponse(request, "events.html", {
        "events": events, "page": page, "total_pages": total_pages,
        "per_page": per_page, "q": q, "severity_filter": severity,
        "correlation_id_filter": correlation_id,
    })


@router.get("/ledger", response_class=HTMLResponse)
async def ledger_page(
    request: Request, page: int = 1, per_page: int = 25,
    q: str = "", severity: str = "", card_type: str = "", app: str = "",
    view: str = "", needs_you: str = "1",
) -> HTMLResponse:
    """Fleet-wide Ledger view (docs/ledger-design-spec.md §5 Phase 1, plus
    the §2 noise-at-scale controls): a read-only union of events/gates/
    deliveries/fix-reviews across every app, additive alongside
    Events/Decisions -- neither of those pages changes.

    Default rendering is §2 rule 2's grouped-by-app view (one row per app,
    collapsed to its most-recent/most-severe card) with rule 3's "Needs
    You" filter on by default. ``?app=`` (a grouped row's own expand link)
    or ``?view=flat`` opts into the flat, chronological stream -- the exact
    same shape Assessment Detail's own Ledger tab renders, just reachable
    fleet-wide too.
    """
    from agentit.ledger import get_ledger_cards, group_cards_by_app, recent_watcher_failures
    from agentit.portal.routes.fleet import _attach_pending_actions

    s = await get_store()
    all_cards = await get_ledger_cards(s, target_app=app or None, limit=2000)

    # Same target_app -> assessment_id resolution events_page already does,
    # so a fleet-wide card can link back to that app's own Assessment
    # Detail / Ledger tab.
    fleet = await s.get_fleet_data()
    app_ids_by_name = {app_data["repo_name"]: app_data["id"] for app_data in fleet}
    for c in all_cards:
        c["assessment_id"] = c.get("assessment_id") or app_ids_by_name.get(c.get("target_app"))

    # §2 rule 3's 4th signal is fleet-wide (a tick isn't scoped to one app),
    # so it's computed once here, before any app/card-level filter narrows
    # `all_cards` down to a single app's stream.
    watcher_alerts = recent_watcher_failures(all_cards, hours=4)

    if q:
        ql = q.lower()
        all_cards = [
            c for c in all_cards
            if ql in (c.get("target_app") or "").lower()
            or ql in c.get("title", "").lower()
            or ql in c.get("summary", "").lower()
        ]
    if severity:
        all_cards = [c for c in all_cards if c.get("severity") == severity]
    if card_type:
        all_cards = [c for c in all_cards if c["card_type"] == card_type]

    if app or view == "flat":
        total = len(all_cards)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        cards = all_cards[start:start + per_page]
        return get_templates().TemplateResponse(request, "ledger.html", {
            "mode": "flat", "cards": cards, "page": page, "total_pages": total_pages,
            "per_page": per_page, "total": total,
            "q": q, "severity_filter": severity, "card_type_filter": card_type,
            "app_filter": app, "watcher_alerts": watcher_alerts,
        })

    # §2 rule 2: fleet-wide default is grouped by app, collapsed to one row
    # each -- score and pending-action count, plus that app's own most-
    # recent Ledger card. Deliberately skips fleet.html's live cluster/Argo
    # CD enrichment (`_enrich_fleet_with_cluster_status`) -- that call
    # bridges a worker thread back onto this request's event loop via
    # `run_coroutine_threadsafe` (see its own docstring), and this grouped
    # view already runs `list_slos()` per app just below; stacking a second
    # thread-bridged cluster call on top added real, measured extra load on
    # that bridge under the full test suite for a GitOps badge the "Needs
    # You" triage view doesn't need (a user who clicks through to Fleet or
    # Assessment Detail already sees it there). If a live deploy-status
    # badge is wanted here later, do it as its own follow-up, not bundled
    # into this view by default.
    await _attach_pending_actions(fleet, s)
    stale_gate_ids = {g["id"] for g in await s.get_stale_gates(hours=4)}
    all_gates = await s.list_all_gates()
    stale_repo_urls = {g["repo_url"] for g in all_gates if g["id"] in stale_gate_ids and g.get("repo_url")}

    cards_by_app = group_cards_by_app(all_cards)
    card_filter_active = bool(q or severity or card_type)
    rows = []
    for a in fleet:
        slos = await s.list_slos(a["id"])
        breached_count = sum(1 for sl in slos if sl.get("status") == "breached")
        app_cards = cards_by_app.get(a["repo_name"], [])
        if card_filter_active and not app_cards:
            continue
        row_needs_you = (
            a.get("pending_actions_count", 0) > 0
            or a["repo_url"] in stale_repo_urls
            or breached_count > 0
        )
        rows.append({
            "repo_name": a["repo_name"],
            "assessment_id": a["id"],
            "score": a["latest_score"],
            "pending_actions_count": a.get("pending_actions_count", 0),
            "breached_slo_count": breached_count,
            "needs_you": row_needs_you,
            "latest_card": app_cards[0] if app_cards else None,
            "card_count": len(app_cards),
            "cards": app_cards[:10],
        })

    total_apps = len(rows)
    if needs_you != "0":
        rows = [r for r in rows if r["needs_you"]]
    rows.sort(key=lambda r: (r["latest_card"]["timestamp"] if r["latest_card"] else ""), reverse=True)

    return get_templates().TemplateResponse(request, "ledger.html", {
        "mode": "grouped", "rows": rows, "total_apps": total_apps,
        "needs_you_count": len(rows), "needs_you_filter": needs_you,
        "q": q, "severity_filter": severity, "card_type_filter": card_type,
        "app_filter": app, "watcher_alerts": watcher_alerts,
    })


@router.get("/ledger/chain/{correlation_id}", response_class=HTMLResponse)
async def ledger_chain_page(request: Request, correlation_id: str) -> HTMLResponse:
    """The rewind scrubber (docs/ledger-design-spec.md §4 -- the non-
    speculative "replay history" half only). Read-only: every card here
    already happened, so none of them render action buttons."""
    from agentit.ledger import get_chain_cards

    s = await get_store()
    cards = await get_chain_cards(s, correlation_id)
    return get_templates().TemplateResponse(request, "ledger_chain.html", {
        "cards": cards, "correlation_id": correlation_id,
    })


@router.get("/events/dlq", response_class=HTMLResponse)
async def dlq_page(request: Request) -> HTMLResponse:
    """Show dead-lettered messages from the events store."""
    s = await get_store()
    dlq_messages = await s.list_dlq_messages()
    return get_templates().TemplateResponse(request, "dlq.html", {"dlq_messages": dlq_messages})


@router.post("/events/dlq/{event_id}/retry")
async def dlq_retry(event_id: str):
    s = await get_store()
    if not await s.retry_dlq_message(event_id):
        return RedirectResponse(url="/events/dlq?error=Message+not+found+or+already+processed", status_code=303)
    return RedirectResponse(url="/events/dlq?success=Message+queued+for+retry", status_code=303)


@router.post("/events/dlq/{event_id}/dismiss")
async def dlq_dismiss(event_id: str):
    s = await get_store()
    if not await s.dismiss_dlq_message(event_id):
        return RedirectResponse(url="/events/dlq?error=Message+not+found+or+already+processed", status_code=303)
    return RedirectResponse(url="/events/dlq?success=Message+dismissed", status_code=303)


@router.post("/events/dlq/dismiss-all")
async def dlq_dismiss_all():
    s = await get_store()
    count = await s.dismiss_all_dlq()
    return RedirectResponse(url=f"/events/dlq?success=Dismissed+{count}+messages", status_code=303)


@router.get("/api/events")
async def api_events(limit: int = 50, target_app: str | None = None):
    s = await get_store()
    events = await s.list_events(limit=limit, target_app=target_app)
    # Enrich with assessment_id (same fleet lookup as the Events page) so
    # the masthead drawer can deep-link to Assessment Actions / Ledger
    # Needs You instead of only correlation filters.
    fleet = await s.get_fleet_data()
    app_ids_by_name = {app_data["repo_name"]: app_data["id"] for app_data in fleet}
    for e in events:
        e["assessment_id"] = app_ids_by_name.get(e.get("target_app"))
    return JSONResponse(events)
