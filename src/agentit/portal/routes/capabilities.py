"""Capabilities catalog (skills/checks), onboarding agents, and watchers.

The Capabilities page is tabbed with Agents in the UI (see README's Web
portal section), so the agent registry routes live alongside the skill
catalog / learning-agent routes here rather than in their own module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time as _time
from datetime import datetime, timezone
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
# `threading.Lock` (not `asyncio.Lock`) so this stays correct regardless of
# whether these disk-reading helpers are ever called synchronously (as
# today) or offloaded to a worker thread via `asyncio.to_thread` (as
# fleet.py/health.py's own TTL caches already are) -- a plain `threading.Lock`
# works fine either way, without requiring these to become `async def`.
_skills_cache_lock = threading.Lock()
_checks_cache_lock = threading.Lock()


def _cached_skills():
    with _skills_cache_lock:
        if _skills_cache["data"] is None or _time.monotonic() - _skills_cache["ts"] > _CACHE_TTL:
            from agentit.skill_engine import load_all_skills
            _skills_cache["data"] = load_all_skills(Path("skills"))
            _skills_cache["ts"] = _time.monotonic()
        return _skills_cache["data"]


def _cached_checks():
    with _checks_cache_lock:
        if _checks_cache["data"] is None or _time.monotonic() - _checks_cache["ts"] > _CACHE_TTL:
            from agentit.check_engine import load_checks
            _checks_cache["data"] = load_checks(Path("checks"))
            _checks_cache["ts"] = _time.monotonic()
        return _checks_cache["data"]


# ── Agents ────────────────────────────────────────────────────────────

# Same 2-day staleness threshold Schedules' watcher table and Capabilities'
# Self-Improvement tab already use (see `_SKILL_LEARNER_STALE_SECONDS`/
# `_CAPABILITY_SCOUT_STALE_SECONDS` below and schedules.py's
# `_WATCHER_STALE_SECONDS`) -- reused here so the Agent Registry/Agent
# Detail pages read off the same real liveness signal instead of the
# `agent_registry.status` column, which `register_agent()`/
# `agent_heartbeat()` only ever write as `'active'` (no code path ever
# writes anything else, so it's true of every row by construction and
# proves nothing about whether the agent is still alive). Keeping every
# page on this one threshold means they can't disagree about the same
# agent's status.
_AGENT_STALE_SECONDS = 2 * 86400


def _agent_display_status(agents: list[dict], agent_name: str) -> str:
    """Real display status for an Agent Registry row/detail page, derived
    from heartbeat age via `watcher_heartbeat_status()` (defined below) --
    "active" (heartbeat within `_AGENT_STALE_SECONDS`), "stale" (heartbeat
    older than that), or "never ticked" (no heartbeat recorded at all).
    Mirrors `schedules.py`'s watcher-table status convention exactly.
    """
    hb = watcher_heartbeat_status(agents, agent_name, _AGENT_STALE_SECONDS)
    if not hb["has_run"]:
        return "never ticked"
    return "stale" if hb["stale"] else "active"


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

    # Real liveness, not the always-'active' registry column -- see
    # `_agent_display_status()` above.
    for a in agents:
        a["status"] = _agent_display_status(agents, a["agent_name"])

    agent_stats = {a["agent"]: a for a in (await s.get_agent_stats())} if hasattr(s, 'get_agent_stats') else {}

    active = sum(1 for a in agents if a["status"] == "active")
    last_hb = max((a["last_heartbeat"] or "" for a in agents), default="—")
    return get_templates().TemplateResponse(request, "agents.html", {
        "agents": agents,
        "total": len(agents),
        "active": active,
        # Full ISO timestamp (not pre-sliced) so the stat card can use the
        # same `data-timestamp` relative-time rendering the table's own
        # "Last Heartbeat" column already uses below -- previously this
        # rendered as a raw ISO string while the table cell for the exact
        # same data correctly showed "26m ago".
        "last_heartbeat": last_hb,
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

    # Real liveness, not the always-'active' registry column -- see
    # `_agent_display_status()` above.
    agent["status"] = _agent_display_status(agents, agent_name)

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

_LEARNING_RUN_MODE_LABELS = {
    "skill-improvement": "Flagged skill improvement",
    "cve-sweep": "CVE sweep",
}

# Matches the AgentITWatcherStale Prometheus alert's own convention (2x a
# watcher's expected interval) -- skill-learner's default/live interval is
# 24h, so a heartbeat older than this means the watcher is very likely
# disabled or stalled, not "just about to tick".
_SKILL_LEARNER_STALE_SECONDS = 2 * 86400


def watcher_heartbeat_status(agents: list[dict], agent_name: str, stale_seconds: int) -> dict:
    """Real status of a long-lived watcher, derived from its own tick
    heartbeat (``agent_heartbeat()``, written by
    ``watchers/__init__.py::record_tick`` after every loop iteration) --
    never a deployment-ready/chart-intent default, which only proves the
    pod is up, not that the watcher's own loop has ever actually ticked.
    Shared by ``_get_skill_learner_status``/``_get_capability_scout_status``
    below and by ``routes/schedules.py``'s "Long-Lived Agents" table, so
    the two pages read off the same real signal and can't disagree about
    the same watcher's status.

    ``agents`` is the caller's already-fetched ``store.list_agents()``
    result -- passed in rather than fetched here so callers keep their own
    error handling/logging around that call.
    """
    agent = next((a for a in agents if a.get("agent_name") == agent_name), None)
    last_heartbeat = agent.get("last_heartbeat") if agent else None
    if not last_heartbeat:
        return {"has_run": False, "last_heartbeat": None, "stale": None}

    try:
        last = datetime.fromisoformat(last_heartbeat)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - last).total_seconds()
    except ValueError:
        return {"has_run": False, "last_heartbeat": None, "stale": None}

    return {
        "has_run": True,
        "last_heartbeat": last_heartbeat,
        "stale": age_seconds > stale_seconds,
    }


async def _get_learning_run_history(s, limit: int = 15) -> list[dict]:
    """Durable run history for the learning agent -- every manual button
    click AND every skill-learner watcher tick that reached a real outcome
    (success, no-op skip, or failure), not just the ones that generated a
    skill. Both entry points log the same ``learning-run`` action (see
    ``learning_agent.describe_learning_run``), so this single query covers
    both without needing to know which one fired.
    """
    if not hasattr(s, "list_events_by_action"):
        return []
    try:
        raw_events = await s.list_events_by_action("learning-run", limit=limit)
    except Exception:
        log.warning("Failed to fetch learning-run history", exc_info=True)
        return []

    runs = []
    for ev in raw_events:
        try:
            details = json.loads(ev.get("details_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        mode = details.get("mode")
        runs.append({
            "timestamp": ev.get("timestamp"),
            "trigger": "Automatic (24h watcher)" if ev.get("agent_id") == "skill-learner" else "Manual",
            "researched": _LEARNING_RUN_MODE_LABELS.get(mode, "—"),
            "severity": ev.get("severity", "info"),
            "summary": ev.get("summary", ""),
        })
    return runs


async def _get_skill_learner_status(s) -> dict:
    """Real status of the skill-learner watcher, derived from its own tick
    heartbeat (``agent_heartbeat("skill-learner")``, written by
    ``watchers/__init__.py::record_tick`` after every loop iteration) --
    not from ``chart/values.yaml``'s ``agents.skillLearner.enabled`` default,
    which the portal process has no reliable way to read at runtime (the
    live deployment overrides that default via ``argocd/application.yaml``,
    so the chart default alone would be actively misleading here). No
    heartbeat ever recorded means either the watcher has never ticked yet
    or it's disabled -- this is genuinely ambiguous from the portal's own
    data, so the returned dict says exactly that rather than guessing.
    """
    if not hasattr(s, "list_agents"):
        return {"has_run": False, "last_heartbeat": None, "stale": None}
    try:
        agents = await s.list_agents()
    except Exception:
        log.warning("Failed to fetch agent registry for skill-learner status", exc_info=True)
        return {"has_run": False, "last_heartbeat": None, "stale": None}

    return watcher_heartbeat_status(agents, "skill-learner", _SKILL_LEARNER_STALE_SECONDS)


async def _get_capability_run_history(s, limit: int = 15) -> list[dict]:
    """Durable run history for capability-scout -- every 24h tick leaves a
    row here, whether or not it produced a proposal (proposed / gate-blocked
    / no-signal), exactly like ``_get_learning_run_history`` above but keyed
    off the ``capability-run`` action instead of ``learning-run``. See
    docs/self-improvement-for-agentit.md's "Portal transparency" section.
    """
    if not hasattr(s, "list_events_by_action"):
        return []
    from agentit.capability_scout import CAPABILITY_RUN_ACTION
    try:
        raw_events = await s.list_events_by_action(CAPABILITY_RUN_ACTION, limit=limit)
    except Exception:
        log.warning("Failed to fetch capability-run history", exc_info=True)
        return []

    runs = []
    for ev in raw_events:
        try:
            details = json.loads(ev.get("details_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        runs.append({
            "event_id": ev.get("id"),
            "timestamp": ev.get("timestamp"),
            "trigger": "Manual" if details.get("trigger") == "manual" else "Automatic (24h watcher)",
            "considered": details.get("evidence") or details.get("doc_anchor") or "—",
            "severity": ev.get("severity", "info"),
            "summary": ev.get("summary", ""),
            "pr_url": details.get("pr_url"),
            "outcome": _capability_run_outcome_label(ev, details),
            "cited_merges": details.get("cited_merges") or [],
        })

    pr_urls = [r["pr_url"] for r in runs if r.get("pr_url")]
    if pr_urls:
        from agentit.portal.github_pr import get_pr_status
        statuses = await asyncio.gather(*(asyncio.to_thread(get_pr_status, url) for url in pr_urls))
        status_map = dict(zip(pr_urls, statuses))
        for r in runs:
            if r.get("pr_url"):
                r["pr_status"] = status_map.get(r["pr_url"], {})
    return runs


def _capability_run_outcome_label(event: dict, details: dict) -> str:
    """Stable outcome badge key for the Self-Improvement UI (L4 loop story)."""
    explicit = str(details.get("outcome") or "").strip()
    if explicit:
        return explicit
    if details.get("pr_url"):
        return "proposed"
    if event.get("severity") == "error" or details.get("error"):
        return "error"
    summary = str(event.get("summary") or "")
    if "gate-blocked" in summary:
        return "gate-blocked"
    if "insufficient real signal" in summary:
        return "no-signal"
    if "no evidence-grounded" in summary or "No proposal this cycle" in summary:
        return "no-proposal"
    return "no-op"


async def _get_recent_capability_outcomes(s, limit: int = 10) -> list[dict]:
    """Recent ``capability-outcome`` rows for the Self-Improvement L4 panel."""
    if not hasattr(s, "list_events_by_action"):
        return []
    from agentit.capability_scout import CAPABILITY_OUTCOME_ACTION, proposal_outcomes_from_events
    try:
        raw = await s.list_events_by_action(CAPABILITY_OUTCOME_ACTION, limit=limit)
    except Exception:
        log.warning("Failed to fetch capability-outcome history", exc_info=True)
        return []
    return proposal_outcomes_from_events(raw)


_CAPABILITY_SCOUT_STALE_SECONDS = 2 * 86400


async def _get_capability_scout_status(s) -> dict:
    """Real status of the capability-scout watcher, derived from its own
    tick heartbeat -- see ``_get_skill_learner_status`` above for the
    identical rationale (chart defaults aren't a reliable signal of what's
    actually running on a live deployment)."""
    if not hasattr(s, "list_agents"):
        return {"has_run": False, "last_heartbeat": None, "stale": None}
    try:
        agents = await s.list_agents()
    except Exception:
        log.warning("Failed to fetch agent registry for capability-scout status", exc_info=True)
        return {"has_run": False, "last_heartbeat": None, "stale": None}

    return watcher_heartbeat_status(agents, "capability-scout", _CAPABILITY_SCOUT_STALE_SECONDS)


@router.get("/capabilities/self-improvement", response_class=HTMLResponse)
async def self_improvement_page(request: Request) -> HTMLResponse:
    """The Self-Improvement tab -- run history for the capability-scout
    watcher, mirroring the Catalog tab's "Learning Agent Runs" table. See
    docs/self-improvement-for-agentit.md's "Portal transparency" section."""
    s = await get_store()
    runs = await _get_capability_run_history(s)
    status = await _get_capability_scout_status(s)
    recent_outcomes = await _get_recent_capability_outcomes(s)
    cited_merges = [o for o in recent_outcomes if o.get("state") == "merged"][:5]
    return get_templates().TemplateResponse(request, "self_improvement.html", {
        "runs": runs,
        "capability_scout_status": status,
        "recent_outcomes": recent_outcomes,
        "cited_merges": cited_merges,
    })


_CAPABILITY_SCOUT_OUTCOME_MESSAGES: dict[str, str] = {
    "no-signal": "No proposal this cycle — insufficient real signal yet.",
    "no-proposal": "No proposal this cycle — LLM found no evidence-grounded gap worth proposing.",
    "gate-blocked": "Proposal generated but blocked by a safety gate — see Self-Improvement Runs below.",
    "pr-failed": "Proposal passed gates but PR creation failed — see Self-Improvement Runs below.",
}


@router.post("/capabilities/self-improvement/run", response_model=None)
async def self_improvement_run_route(request: Request):
    """Manually trigger one capability-scout cycle right now, instead of
    waiting up to 24h for its watcher tick -- portal parity with
    ``/capabilities/learn``'s manual trigger for skill-learner (the gap
    docs/ui-redesign-proposal.md §5 flags). Calls
    ``CapabilityScout.research_once()`` synchronously, mirroring
    ``/capabilities/learn``'s existing synchronous-call shape (including
    the Route's ``haproxy.router.openshift.io/timeout: 200s`` annotation
    already accommodating a similarly long-running call).
    """
    from agentit.events import get_publisher
    from agentit.watchers.capability_scout import CapabilityScout

    s = await get_store()
    scout = CapabilityScout(publisher=get_publisher(), store=s, repo_dir=Path.cwd())

    try:
        result = await with_timeout(scout.research_once(trigger="manual"), timeout=180)
    except Exception as exc:
        log.exception("Manual capability-scout run failed")
        return RedirectResponse(
            url=f"/capabilities/self-improvement?error={quote(f'Self-improvement scan failed: {exc}'[:200])}",
            status_code=303,
        )

    outcome = result.get("outcome", "unknown")
    if outcome == "proposed":
        return RedirectResponse(
            url=f"/capabilities/self-improvement?success={quote('Opened proposal PR: ' + result.get('pr_url', ''))}",
            status_code=303,
        )
    if outcome == "no-llm":
        return RedirectResponse(
            url=(
                f"/capabilities/self-improvement?error="
                f"{quote('LLM unavailable — set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID to enable.')}"
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=(
            f"/capabilities/self-improvement?warning="
            f"{quote(_CAPABILITY_SCOUT_OUTCOME_MESSAGES.get(outcome, 'Scan completed: ' + outcome))}"
        ),
        status_code=303,
    )


@router.get("/capabilities/self-improvement/runs/{event_id}", response_class=HTMLResponse)
async def capability_run_detail(request: Request, event_id: str) -> HTMLResponse:
    """Per-run drill-through -- evidence, per-gate pass/fail table, and a
    live PR status badge, mirroring ``skill_history``'s layout above."""
    s = await get_store()
    event = await s.get_event(event_id) if hasattr(s, "get_event") else None
    if event is None:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        details = json.loads(event.get("details_json") or "{}")
    except (TypeError, ValueError):
        details = {}

    pr_url = details.get("pr_url")
    pr_status: dict = {}
    if pr_url:
        from agentit.portal.github_pr import get_pr_status
        pr_status = await asyncio.to_thread(get_pr_status, pr_url)

    outcome = _capability_run_outcome_label(event, details)
    return get_templates().TemplateResponse(request, "capability_run_detail.html", {
        "event": event,
        "details": details,
        "pr_url": pr_url,
        "pr_status": pr_status,
        "outcome": outcome,
        "cited_merges": details.get("cited_merges") or [],
        "proposal_outcomes": details.get("proposal_outcomes") or [],
    })


@router.get("/capabilities", response_class=HTMLResponse)
async def capabilities_page(request: Request) -> HTMLResponse:
    from agentit.remediation.registry import FIX_REGISTRY

    skills = _cached_skills()
    checks = _cached_checks()

    s = await get_store()
    effectiveness = await s.get_skill_effectiveness()
    recent_activity = await s.get_recent_skill_activity(limit=20)
    catalog_changes = await s.list_events_by_agent("skill-inventory", limit=10)

    flagged_skills: list[dict] = []
    if hasattr(s, "get_low_effectiveness_skills"):
        try:
            flagged_skills = await s.get_low_effectiveness_skills()
        except Exception:
            log.warning("Failed to fetch low-effectiveness skills for learn-button preview", exc_info=True)
    learning_runs = await _get_learning_run_history(s)
    skill_learner_status = await _get_skill_learner_status(s)
    llm_available = get_llm_client() is not None

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
        "flagged_skills": flagged_skills,
        "learning_runs": learning_runs,
        "skill_learner_status": skill_learner_status,
        "llm_available": llm_available,
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
    from agentit.learning_agent import LEARNING_RUN_ACTION, describe_learning_run

    s = await get_store()
    llm_client = get_llm_client()
    if llm_client is None:
        error_msg = "LLM unavailable — set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID to enable skill research."
        severity, summary, details = describe_learning_run("manual", None, [], [], error=error_msg)
        await s.log_event("learning-agent", LEARNING_RUN_ACTION, None, severity, summary, details=details)
        return RedirectResponse(
            url=f"/capabilities?error={quote(error_msg)}",
            status_code=303,
        )

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
        mode = "skill-improvement" if flagged else "cve-sweep"
        severity, summary, details = describe_learning_run("manual", mode, [], [], error=str(exc)[:200])
        await s.log_event("learning-agent", LEARNING_RUN_ACTION, None, severity, summary, details=details)
        return RedirectResponse(
            url=f"/capabilities?error={quote(f'Skill research failed: {exc}'[:200])}",
            status_code=303,
        )

    mode = "skill-improvement" if improved else "cve-sweep"
    severity, summary, details = describe_learning_run("manual", mode, saved, skipped)
    await s.log_event("learning-agent", LEARNING_RUN_ACTION, None, severity, summary, details=details)

    if saved:
        _skills_cache["data"] = None  # bust the 60s cache so new skills show immediately
        # Kept alongside the LEARNING_RUN_ACTION event above for backward
        # compatibility -- existing consumers (tests, the toast) key off
        # "skills-generated" specifically to know a skill was actually written.
        await s.log_event("learning-agent", "skills-generated", None, "info", summary)
        return RedirectResponse(url=f"/capabilities?success={quote(summary)}", status_code=303)

    # Nothing generated (all skipped, or research returned nothing usable) --
    # still a real outcome worth surfacing, but not the same as "it worked",
    # so this uses the warning toast rather than a misleadingly green success one.
    return RedirectResponse(url=f"/capabilities?warning={quote(summary)}", status_code=303)


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


def _persist_skill_activation(target: Path, repo_dir: Path) -> dict:
    """Commit `target`'s already-written draft->active flip to git and open
    a draft PR for it -- reusing the exact branch/commit/push
    (`git_pr.create_branch_commit_push`) and PR-open (`git_pr.open_draft_pr`)
    mechanics `capability_scout.py`'s `_open_pr()` already uses for changes
    to AgentIT's own repo, rather than inventing a new persistence path.

    Never a direct commit to `main`: every existing automated flow that
    touches this repo (`self-fix --create-pr`, capability-scout) pushes a
    branch/opens a PR instead, and capability_scout.py's own module
    docstring states that convention explicitly ("Never a direct commit to
    `main`, never auto-merge") -- followed here for consistency, even
    though this specific change (flipping one YAML field) is lower-risk
    than either of those. (`main` has no GitHub branch-protection rule
    enforcing this technically, but the codebase's own precedent already
    settles it.)

    Without this, `activate_skill_route`'s `target.write_text(...)` only
    ever lands in the live pod's writable container layer -- `skills/` is
    baked into the image at build time (no PVC/volume mount), so a
    redeploy silently reverts every activation. Once the resulting PR
    merges, the next redeploy bakes in the already-active state instead of
    reverting it.

    Returns ``{"pr_url": ...}`` on success or ``{"error": ...}`` on
    failure -- never raises, matching `_open_pr`'s contract, since a
    git/network/auth failure here must not undo (or crash) an activation
    that's already live in the pod.
    """
    from agentit.git_pr import create_branch_commit_push, open_draft_pr

    try:
        rel_path = str(target.relative_to(repo_dir))
    except ValueError:
        rel_path = str(target)

    branch = f"agentit/activate-skill/{target.stem}-{int(_time.time())}"
    commit_message = (
        f"chore(skills): activate {target.stem}\n\n"
        f"Promotes {rel_path} from status: draft to status: active via the "
        f"Capabilities UI. Without this commit the flip only lives in the "
        f"running pod's writable layer and is silently reverted by the next "
        f"redeploy, since skills/ is baked into the container image."
    )
    branch_result = create_branch_commit_push(branch, [rel_path], commit_message, cwd=repo_dir)
    if not branch_result.get("success"):
        return {"error": branch_result.get("error", "git branch/commit/push failed")}

    body = (
        f"## Activate skill: {target.stem}\n\n"
        f"Promotes `{rel_path}` from `status: draft` to `status: active`.\n\n"
        "The running pod's copy was already flipped (immediately usable); "
        "this PR makes that survive the next redeploy, since `skills/` is "
        "baked into the container image and isn't backed by a volume.\n\n"
        "> Opened by AgentIT's Capabilities UI activation flow."
    )
    return open_draft_pr(
        branch=branch, title=f"[AgentIT] Activate skill: {target.stem}", body=body, cwd=repo_dir,
    )


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

    # In-pod activation is done and already usable regardless of what
    # happens next -- see `_persist_skill_activation`'s docstring for why
    # this git step still has to run (skills/ is baked into the image, no
    # volume mount) and why its failure must not be swallowed silently.
    git_result = await asyncio.to_thread(_persist_skill_activation, target, Path.cwd())
    pr_url = git_result.get("pr_url")

    success_msg = f"Activated: {target.stem}"
    if verify_warnings:
        success_msg += f" (note: {'; '.join(verify_warnings)})"
    if pr_url:
        success_msg += f" — persisted via PR: {pr_url}"

    event_summary = f"Activated skill: {target.stem}" + (f" ({'; '.join(verify_warnings)})" if verify_warnings else "")
    event_details: dict = {"skill": target.stem}
    if pr_url:
        event_details["pr_url"] = pr_url
    else:
        event_details["git_persist_error"] = git_result.get("error", "unknown error")
    await s.log_event(
        "portal", "skill-activated", None, "info" if pr_url else "warning",
        event_summary, details=event_details,
    )

    url = f"/capabilities?success={quote(success_msg)}"
    if not pr_url:
        # The pod-local flip already happened and is usable now, but with
        # no git trace anywhere it will be silently reverted by the next
        # redeploy exactly like the pre-fix bug -- this must surface as a
        # visible warning, not a swallowed failure, per the confirmed
        # CVE-mitigation-skill incidents this fix addresses.
        warning_msg = (
            f"{target.stem} is active in this pod now, but the git commit needed to survive the "
            f"next redeploy failed ({str(git_result.get('error', 'unknown error'))[:150]}) — it will be "
            f"silently reverted on redeploy unless this is retried or committed manually."
        )
        url += f"&warning={quote(warning_msg)}"
    return RedirectResponse(url=url, status_code=303)
