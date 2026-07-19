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
# the CI/CD-shared-namespace variant of it (delivery.py), "rollback-review"
# (watchers/slo_tracker.py), and the "finding-" prefix, which covers both
# delivery.py's ESCALATION_GATE_TYPE ("finding-unresolved-escalation",
# Phase 4) and routes/webhooks.py's per-category dispatcher gates
# ("finding-{category}"). Direct Apply and its "cluster-conflict-review"
# gate are already gone (routes/gates.py); ADMIN_REVIEW_GATE_TYPE
# ("cluster-admin-review") joined them 2026-07-18, and "auto-mode-review"
# joined them alongside AutoMode's removal -- no code path creates either
# anymore, but both are kept in this set deliberately (unlike the other
# retired types, which were simply dropped from here): a real,
# still-pending row of either type may still exist in production, and it
# still resolves to a genuine GitOps PR when approved (see routes/gates.py's
# generic fallback) -- a real, actionable pending item should keep counting
# here until it's actually resolved, not silently stop counting just
# because its type can no longer be freshly created.
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
    fix-review (skill_effectiveness), secret-classify, and capability-proposal
    (both via events) decision records into one view. See agentit/
    llm_decisions.py's module docstring for what each decision type covers
    and how it's attributed.
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
    # assessment_id resolves (Fleet and Decisions already link app names
    # the same way; Events showed plain text).
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
    q: str = "", category: str = "", app: str = "", lifecycle: str = "",
) -> HTMLResponse:
    """Ledger's whole job (product direction, superseding the earlier
    generic A-P event-union design in docs/ledger-design-spec.md): a
    fleet-wide list of every PR AgentIT has ever opened, across every app,
    with each PR's real lifecycle -- waiting for your approval, merged,
    rejected (with the real reason), or closed -- filterable by category
    and app. "Which PRs need my attention, and what happened to every PR",
    not a generic system-activity feed.

    "Waiting for your approval" means exactly what it says: any PR that's
    still open and unmerged on GitHub (``pr_tracking.
    fleet_prs_waiting_for_approval()``), whether or not AgentIT happens to
    have an in-app approval gate for it. Gate-tracked ones additionally
    get the real Approve & Deliver/Reject actions rendered inline; every
    other open PR is a plain pointer to review it on GitHub -- but it
    still counts here, because it's still genuinely waiting on a human
    (2026-07-19: this used to require a gate, silently undercounting every
    source-repo-pr/app-repo-pr/onboarding PR, which never gets one).

    Deliberately NOT a replacement for:

    - **Events** (``/events``) -- the real-time, behind-the-scenes system-
      activity feed (watcher ticks, webhook-triggered re-assessments, drift
      detection, catalog changes, ...) regardless of whether a PR is
      involved at all. Every one of the old card types that was genuinely
      "the system did a thing" rather than "a PR needs attention or
      changed status" is already a raw ``events`` row today and already
      fully visible there -- nothing moved out of this page had nowhere
      else to go.
    - **Decisions** (``/decisions``) -- the LLM decision audit, including
      fix-review (``skill_effectiveness``) outcomes.
    - Non-PR gate types (``cluster-admin-review``, ``rollback-review``,
      ``finding-unresolved-escalation``) -- these were never PRs; they stay
      visible via Admin Review, Fleet's per-app "needs action"/escalation
      badges, and Assessment Detail's own Actions tab.
    - Per-app history -- Assessment Detail's Timeline/Ledger tabs and PR
      History tab still own "everything that happened to this one app".

    ``pr_tracking.collect_fleet_pr_records()`` does the real aggregation
    (from ``gates``/``deliveries``/``onboarding_results``, the same three
    places Fleet's "Open PRs"/"Total PRs" columns and Assessment Detail's
    PR History tab already read) -- this route only filters/paginates/
    renders what that function returns.
    """
    from agentit.portal.pr_tracking import collect_fleet_pr_records, fleet_prs_waiting_for_approval

    s = await get_store()
    records = await collect_fleet_pr_records(s)

    # Filter option lists reflect the full, unfiltered fleet -- so picking
    # one filter never hides the others still available to pick next.
    categories = sorted({r["category"] for r in records if r.get("category")})
    apps = sorted({r["app_name"] for r in records if r.get("app_name")})

    if q:
        ql = q.lower()
        records = [
            r for r in records
            if ql in (r.get("app_name") or "").lower()
            or ql in (r.get("category") or "").lower()
            or ql in (r.get("title") or "").lower()
            or ql in (r.get("pr_url") or "").lower()
        ]
    if category:
        records = [r for r in records if r.get("category") == category]
    if app:
        records = [r for r in records if r.get("app_name") == app]
    if lifecycle == "needs_approval":
        # "Waiting for approval" is purely PR-status-derived (see
        # fleet_prs_waiting_for_approval()) -- filter by real state, not by
        # the narrower gate-tracked `lifecycle` value, so this option
        # matches the section it's named after.
        records = [r for r in records if r["state"] == "open"]
    elif lifecycle:
        records = [r for r in records if r.get("lifecycle") == lifecycle]

    # Every PR that's still open and unmerged on GitHub always renders in
    # full, ungated by pagination -- this is the one bucket the whole page
    # exists to make impossible to miss. Deliberately NOT the narrower
    # `needs_attention` (gate-tracked-and-pending) flag: only the cluster-
    # config/CI-CD-shared-namespace delivery categories ever create an
    # in-app gate at all (see pr_tracking.py's module docstring), so a
    # source-repo-pr/app-repo-pr/onboarding PR that's genuinely open on
    # GitHub used to fall through to the read-only history table below
    # instead of here -- the exact undercount this fixes (2026-07-19).
    # Gate-tracked records still render the real Approve & Deliver/Reject
    # actions via `gate_card` (off each record's own `raw` gate row); every
    # other open PR renders as a plain "review it on GitHub" pointer.
    needs_approval = fleet_prs_waiting_for_approval(records)
    for r in needs_approval:
        if r.get("source") == "gate":
            r["raw"]["severity"] = "warning"

    history = [r for r in records if r["state"] != "open"]
    total_history = len(history)
    total_pages = max(1, (total_history + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    history_page = history[start:start + per_page]

    return get_templates().TemplateResponse(request, "ledger.html", {
        "needs_approval": needs_approval,
        "history": history_page,
        "total_records": len(records),
        "total_history": total_history,
        "page": page, "total_pages": total_pages, "per_page": per_page,
        "categories": categories, "apps": apps,
        "q": q, "category_filter": category, "app_filter": app, "lifecycle_filter": lifecycle,
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
