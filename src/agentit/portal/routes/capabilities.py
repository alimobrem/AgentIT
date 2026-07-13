"""Capabilities catalog (skills/checks), onboarding agents, and watchers.

The Capabilities page is tabbed with Agents in the UI (see README's Web
portal section), so the agent registry routes live alongside the skill
catalog / learning-agent routes here rather than in their own module.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.agents.capabilities import AGENT_CAPABILITIES, WATCHER_AGENTS as _WATCHER_AGENTS
from agentit.portal.helpers import get_llm_client, get_retention_days, get_store, get_templates, with_timeout

log = logging.getLogger(__name__)

router = APIRouter()

_skills_cache: dict = {"data": None, "ts": 0}
_checks_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 60  # seconds


def _cached_skills():
    if _skills_cache["data"] is None or _time.monotonic() - _skills_cache["ts"] > _CACHE_TTL:
        from agentit.skill_engine import load_all_skills
        _skills_cache["data"] = load_all_skills(Path("skills"))
        _skills_cache["ts"] = _time.monotonic()
    return _skills_cache["data"]


def _cached_checks():
    if _checks_cache["data"] is None or _time.monotonic() - _checks_cache["ts"] > _CACHE_TTL:
        from agentit.check_engine import load_checks
        _checks_cache["data"] = load_checks(Path("checks"))
        _checks_cache["ts"] = _time.monotonic()
    return _checks_cache["data"]


# ── Agents ────────────────────────────────────────────────────────────


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    s = await get_store()
    agents = await s.list_agents()

    for a in agents:
        if not a.get("capabilities") or a["capabilities"] in ("[]", ""):
            a["capabilities"] = AGENT_CAPABILITIES.get(a["agent_name"], "")

    # Merge long-lived watcher agents that aren't in the registry
    registered_names = {a["agent_name"] for a in agents}
    for w in _WATCHER_AGENTS:
        if w["name"] not in registered_names:
            agents.append({
                "agent_name": w["name"],
                "category": w["mode"],
                "status": "deployed",
                "capabilities": AGENT_CAPABILITIES.get(w["name"], f"interval: {w['interval']}"),
                "registered_at": "—",
                "last_heartbeat": "—",
            })

    agent_stats = {a["agent"]: a for a in (await s.get_agent_stats())} if hasattr(s, 'get_agent_stats') else {}

    active = sum(1 for a in agents if a["status"] == "active")
    last_hb = max((a["last_heartbeat"] or "" for a in agents), default="—")
    return get_templates().TemplateResponse(request, "agents.html", {
        "agents": agents,
        "total": len(agents),
        "active": active,
        "last_heartbeat": last_hb[:19] if last_hb != "—" else "—",
        "agent_stats": agent_stats,
    })


@router.get("/agents/{agent_name}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_name: str) -> HTMLResponse:
    s = await get_store()
    agents = await s.list_agents()
    agent = next((a for a in agents if a["agent_name"] == agent_name), None)

    # Long-lived agents may not be in the registry -- create a synthetic entry
    if agent is None:
        watcher = next((w for w in _WATCHER_AGENTS if w["name"] == agent_name), None)
        if watcher is not None:
            agent = {
                "agent_name": agent_name,
                "category": watcher["mode"],
                "status": "deployed",
                "capabilities": f"interval: {watcher['interval']}",
                "registered_at": "—",
                "last_heartbeat": "—",
            }
        else:
            raise HTTPException(status_code=404, detail="Agent not found")

    events = await s.list_events_by_agent(agent_name, limit=50)
    remediations = await s.list_remediations_by_agent(agent_name)
    pending = sum(1 for r in remediations if r["status"] not in ("completed", "applied"))
    completed = sum(1 for r in remediations if r["status"] in ("completed", "applied"))
    agent_runs = await s.list_agent_runs(agent_name, limit=50) if hasattr(s, 'list_agent_runs') else []

    return get_templates().TemplateResponse(request, "agent_detail.html", {
        "agent": agent,
        "events": events,
        "remediations": remediations,
        "pending": pending,
        "completed": completed,
        "agent_runs": agent_runs,
    })


@router.get("/api/agents")
async def api_agents(status: str = "active"):
    s = await get_store()
    return JSONResponse(await s.list_agents(status=status))


# ── Workflows ─────────────────────────────────────────────────────────


@router.get("/workflows")
async def workflows_redirect():
    return RedirectResponse(url="/capabilities", status_code=301)


# ── Capabilities ─────────────────────────────────────────────────────


@router.get("/capabilities", response_class=HTMLResponse)
async def capabilities_page(request: Request) -> HTMLResponse:
    from agentit.remediation.registry import FIX_REGISTRY

    skills = _cached_skills()
    checks = _cached_checks()

    s = await get_store()
    effectiveness = await s.get_skill_effectiveness()
    recent_activity = await s.get_recent_skill_activity(limit=20)
    catalog_changes = await s.list_events_by_agent("skill-inventory", limit=10)

    # Group skills by domain
    skills_by_domain: dict[str, list] = {}
    for skill in skills:
        skills_by_domain.setdefault(skill.domain, []).append(skill)

    # Group checks by dimension
    checks_by_dimension: dict[str, list] = {}
    for check in checks:
        checks_by_dimension.setdefault(check.dimension, []).append(check)

    total_skills = len(skills)
    active_skills = sum(1 for sk in skills if sk.status == "active")
    deprecated_skills = sum(1 for sk in skills if sk.status == "deprecated")
    total_checks = len(checks)

    from agentit.agents.capabilities import get_onboarding_agents, WATCHER_AGENTS
    agents = get_onboarding_agents()
    watchers = WATCHER_AGENTS
    fix_categories = [
        {"category": cat, "agent": agent_name, "method": method.lstrip("_").replace("_", " ")}
        for cat, (agent_name, method) in sorted(FIX_REGISTRY.items())
    ]
    retention_days = get_retention_days()

    return get_templates().TemplateResponse(request, "capabilities.html", {
        "skills_by_domain": skills_by_domain,
        "checks_by_dimension": checks_by_dimension,
        "effectiveness": effectiveness,
        "recent_activity": recent_activity,
        "catalog_changes": catalog_changes,
        "total_skills": total_skills,
        "active_skills": active_skills,
        "deprecated_skills": deprecated_skills,
        "total_checks": total_checks,
        "agents": agents,
        "watchers": watchers,
        "fix_categories": fix_categories,
        "retention_days": retention_days,
    })


@router.post("/capabilities/learn", response_model=None)
async def capabilities_learn_route(request: Request):
    """Research low-effectiveness skills first via LLM, falling back to a
    generic CVE sweep when nothing's flagged, and generate new/improved
    skills.

    Portal entry point for what was previously only reachable via the CLI's
    ``agentit learn`` command — the research/skill-generation loop had no UI
    trigger at all before this. Mirrors the same prioritization the
    ``skill-learner`` watcher now does (``watchers/skill_learner.py``'s
    ``research_once()``) so both entry points behave consistently.
    """
    llm_client = get_llm_client()
    if llm_client is None:
        return RedirectResponse(
            url=f"/capabilities?error={quote('LLM unavailable — set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID to enable skill research.')}",
            status_code=303,
        )

    s = await get_store()
    flagged: list[dict] = []
    if hasattr(s, "get_low_effectiveness_skills"):
        try:
            flagged = await s.get_low_effectiveness_skills()
        except Exception:
            log.warning("Failed to fetch low-effectiveness skills", exc_info=True)

    from agentit.learning_agent import (
        check_skill_exists,
        generate_skill_from_research,
        research_cves,
        research_skill_improvement,
        save_skill,
    )
    from agentit.skill_engine import load_all_skills

    def _run() -> tuple[list[str], list[str], bool]:
        """Returns (saved, skipped, improved) -- ``improved`` distinguishes a
        skill-improvement pass from the generic CVE sweep for the success
        message below."""
        saved: list[str] = []
        skipped: list[str] = []
        skills_dir = Path("skills")

        if flagged:
            by_name = {sk.name: sk for sk in load_all_skills(skills_dir)}
            for entry in flagged[:3]:
                skill_name = entry["skill"]
                skill = by_name.get(skill_name)
                if skill is None:
                    skipped.append(skill_name)
                    continue
                item = research_skill_improvement(llm_client, skill.name, skill.domain, entry)
                if not item:
                    skipped.append(skill_name)
                    continue
                content = generate_skill_from_research(llm_client, item, domain=skill.domain)
                if not content:
                    skipped.append(skill_name)
                    continue
                # Deliberately no check_skill_exists() -- an improvement is
                # expected to match the existing (underperforming) skill's
                # name/domain, that's not a duplicate to reject.
                path = save_skill(content, skills_dir, domain=skill.domain)
                if path:
                    saved.append(path.stem)
            if saved or skipped:
                return saved, skipped, True

        for item in research_cves(llm_client, limit=3):
            item_name = item.get("id") or item.get("title") or item.get("name", "")
            if item_name and check_skill_exists(skills_dir, item_name, "security"):
                skipped.append(item_name)
                continue
            content = generate_skill_from_research(llm_client, item, domain="security")
            if not content:
                continue
            path = save_skill(content, skills_dir, domain="security")
            if path:
                saved.append(path.stem)
        return saved, skipped, False

    try:
        saved, skipped, improved = await with_timeout(asyncio.to_thread(_run), timeout=180)
    except Exception as exc:
        log.exception("Skill research failed")
        return RedirectResponse(
            url=f"/capabilities?error={quote(f'Skill research failed: {exc}'[:200])}",
            status_code=303,
        )

    if saved:
        _skills_cache["data"] = None  # bust the 60s cache so new skills show immediately
        kind = "improvement" if improved else "new skill"
        await s.log_event("learning-agent", "skills-generated", None, "info",
                           f"Generated {len(saved)} {kind}(s): {', '.join(saved)}")
        msg = f"Generated {len(saved)} {kind}(s): {', '.join(saved)}"
        if skipped:
            msg += f" ({len(skipped)} skipped)"
    elif skipped:
        msg = (f"No new skills — {len(skipped)} flagged low-effectiveness skill(s) couldn't be improved this time."
               if improved else
               f"No new skills — {len(skipped)} researched CVE(s) already have matching skills.")
    else:
        msg = "No new skills generated — research returned nothing usable this time."
    return RedirectResponse(url=f"/capabilities?success={quote(msg)}", status_code=303)


@router.get("/capabilities/skills/{skill_name}/history", response_class=HTMLResponse)
async def skill_history(request: Request, skill_name: str) -> HTMLResponse:
    """Per-skill lifecycle view: effectiveness trend over time plus
    activation/deprecation history -- the loop-visibility half of the
    self-improvement loop (see README's Self-improvement loop section)."""
    s = await get_store()
    history = await s.get_skill_history(skill_name) if hasattr(s, "get_skill_history") else {"outcomes": [], "events": []}

    skill = next((sk for sk in _cached_skills() if sk.name == skill_name), None)

    effectiveness = None
    if hasattr(s, "get_skill_effectiveness"):
        eff_all = await s.get_skill_effectiveness(skill_name=skill_name, min_count=1)
        effectiveness = eff_all.get(skill_name)

    return get_templates().TemplateResponse(request, "skill_detail.html", {
        "skill_name": skill_name,
        "skill": skill,
        "effectiveness": effectiveness,
        "outcomes": history.get("outcomes", []),
        "lifecycle_events": history.get("events", []),
    })


@router.post("/capabilities/skills/activate", response_model=None)
async def activate_skill_route(request: Request):
    """Promote a draft skill to active. Portal equivalent of `agentit activate-skill`.

    Draft skills are only ever written by the learning agent (research
    button, skill-learner watcher, or CLI) — this is the human-review step
    that lets the skill engine actually start matching them.
    """
    form = await request.form()
    skill_path_raw = str(form.get("skill_path", ""))

    skills_root = Path("skills").resolve()
    try:
        target = Path(skill_path_raw).resolve()
        target.relative_to(skills_root)
    except (ValueError, OSError):
        return RedirectResponse(
            url=f"/capabilities?error={quote('Invalid skill path')}", status_code=303,
        )

    if not target.is_file():
        return RedirectResponse(url=f"/capabilities?error={quote('Skill file not found')}", status_code=303)

    content = target.read_text(encoding="utf-8")
    if "status: draft" not in content:
        return RedirectResponse(
            url=f"/capabilities?error={quote('Skill is not in draft status')}", status_code=303,
        )

    from agentit.skill_engine import load_skill, verify_skill
    skill = load_skill(target)
    if skill is None:
        return RedirectResponse(
            url=f"/capabilities?error={quote('Could not parse skill file — activation blocked')}",
            status_code=303,
        )

    passed, issues, verify_warnings = await asyncio.to_thread(
        verify_skill, skill, llm_client=get_llm_client(),
    )
    s = await get_store()
    if not passed:
        issues_str = "; ".join(issues)
        await s.log_event(
            "portal", "skill-activation-blocked", None, "warning",
            f"Activation blocked for {target.stem}: {issues_str}",
        )
        return RedirectResponse(
            url=f"/capabilities?error={quote(f'Activation blocked — skill failed verification: {issues_str}')}",
            status_code=303,
        )

    target.write_text(content.replace("status: draft", "status: active", 1), encoding="utf-8")
    _skills_cache["data"] = None
    success_msg = f"Activated: {target.stem}"
    if verify_warnings:
        success_msg += f" (note: {'; '.join(verify_warnings)})"
    await s.log_event("portal", "skill-activated", None, "info",
                       f"Activated skill: {target.stem}" + (f" ({'; '.join(verify_warnings)})" if verify_warnings else ""))
    return RedirectResponse(url=f"/capabilities?success={quote(success_msg)}", status_code=303)
