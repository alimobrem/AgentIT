"""Fleet insights, LLM decision audit, event feed, and the events DLQ."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet_insights = await s.get_fleet_insights()
    agent_stats = await s.get_agent_stats()
    feedback = await s.get_all_feedback(limit=10) if hasattr(s, 'get_all_feedback') else []
    low_skills = await s.get_low_effectiveness_skills() if hasattr(s, 'get_low_effectiveness_skills') else []
    check_compliance = await s.get_check_compliance() if hasattr(s, 'get_check_compliance') else []
    loop_health = await s.get_loop_health() if hasattr(s, 'get_loop_health') else None
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
    # a Postgres-backed store's coroutine methods need bridging back onto
    # *this* coroutine's event loop -- pass it the raw sync store directly
    # when available (sqlite; no bridge needed) or `s` + this loop
    # otherwise (postgres; see llm_decisions.py's `_bridge`).
    loop = asyncio.get_running_loop()
    store_arg = s.raw if hasattr(s, "raw") else s
    all_decisions = await asyncio.to_thread(list_llm_decisions, store_arg, 500, loop=loop)
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
    return JSONResponse(await s.list_events(limit=limit, target_app=target_app))
