"""Schedule management endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


# ── Constants ─────────────────────────────────────────────────────────

_CRON_HUMAN = {
    "0 3 1 * *": "Monthly (1st, 3am UTC)",
    "0 4 * * 1": "Weekly (Mon 4am UTC)",
    "0 5 * * 1": "Weekly (Mon 5am UTC)",
    "0 2 * * 3": "Weekly (Wed 2am UTC)",
    "0 6 * * 1": "Weekly (Mon 6am UTC)",
}

_SCHEDULE_FILES = {
    "compliance-cronjob.yaml": ("compliance", "Compliance re-assessment"),
    "cost-cronjob.yaml": ("cost", "Cost optimization report"),
    "dependency-cronjob.yaml": ("dependency", "Dependency scan"),
    "chaos-schedule.yaml": ("chaos", "Chaos experiments"),
}

from agentit.agents.capabilities import WATCHER_AGENTS as _WATCHER_AGENTS
from agentit.portal.routes.capabilities import watcher_heartbeat_status

# Same 2-day staleness threshold Capabilities' Self-Improvement tab already
# uses for skill-learner/capability-scout (watchers/capability_scout.py's
# real tick interval is 24h) -- reused here for every watcher so this table
# and Capabilities can't disagree about the same watcher.
_WATCHER_STALE_SECONDS = 2 * 86400


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request) -> HTMLResponse:
    import yaml as _yaml

    s = await get_store()
    fleet = await s.get_fleet_data()
    schedules: list[dict] = []
    # repo_name -> latest assessment id, used to link "App Name" to that
    # app's Assessment Detail page wherever a real assessment_id can be
    # resolved (every other page -- Fleet, Remediations, Decisions --
    # already does this; Schedules didn't).
    app_ids_by_name = {app_data["repo_name"]: app_data["id"] for app_data in fleet}

    for app_data in fleet:
        aid = app_data["id"]
        files = await s.get_onboarding(aid)
        if not files:
            continue
        for f in files:
            # Skill-generated cronjob files are named "{app_name}-{skill}.yaml"
            # (see skill_engine.py) rather than the bare filenames the removed
            # Python agents wrote -- match by suffix instead of exact name.
            sched_info = next(
                (info for suffix, info in _SCHEDULE_FILES.items() if f["path"].endswith(suffix)),
                None,
            )
            if sched_info is None:
                continue
            agent, desc = sched_info
            try:
                doc = _yaml.safe_load(f["content"])
                cron = doc.get("spec", {}).get("schedule", "unknown")
                concurrency = doc.get("spec", {}).get("concurrencyPolicy", "Allow")
            except (ValueError, AttributeError, _yaml.YAMLError):
                cron = "unknown"
                concurrency = "unknown"
            # No settings-key override applied here (there used to be one,
            # written by this page's own now-removed Save/Enable/Disable
            # controls): that override only ever changed what THIS page
            # displayed -- nothing reads it to patch a live CronJob or to
            # regenerate/redeliver the generated manifest, so honoring it
            # for display would show a schedule that was never actually
            # true anywhere else. Always render the real value straight
            # from the generated manifest -- see docs/unified-apply-flow.md
            # for why a manifest edit has to go through Dry Run + Apply/
            # Commit (or a GitOps PR) to ever become real.
            schedules.append({
                "app_name": app_data["repo_name"],
                "app_id": aid,
                "job_name": desc,
                "schedule": cron,
                "human_schedule": _CRON_HUMAN.get(cron, cron),
                "agent": agent,
                "concurrency": concurrency,
                "enabled": True,
            })

    # Merge manually created schedules from the store
    manual_schedules = await s.list_schedules()
    for ms in manual_schedules:
        schedules.append({
            "id": ms["id"],
            "app_name": ms["app_name"],
            # Manual schedules take a free-text app_name with no
            # guaranteed matching assessment -- only link when one really
            # resolves, never fabricate a target.
            "app_id": app_ids_by_name.get(ms["app_name"]),
            "job_name": ms["job_name"],
            "schedule": ms["schedule"],
            "human_schedule": _CRON_HUMAN.get(ms["schedule"], ms["schedule"]),
            "agent": ms["agent"],
            "concurrency": "Allow",
            "enabled": bool(ms["enabled"]),
            "source": "manual",
        })

    # Tag onboarding-generated schedules with source
    for sched in schedules:
        if "source" not in sched:
            sched["source"] = "onboarding"

    agents = await s.list_agents()
    watchers = []
    for w in _WATCHER_AGENTS:
        # Same real heartbeat source Capabilities' Self-Improvement tab
        # already uses (agent_registry.last_heartbeat, written by
        # watchers/__init__.py::record_tick) -- not deployment-ready
        # status, which only proves the pod is up, not that the watcher's
        # own loop has ever actually ticked (confirmed live: a
        # crash-looping-before-first-tick capability-scout pod still reads
        # "ready" to Kubernetes while Capabilities correctly reports it has
        # never ticked).
        hb_status = watcher_heartbeat_status(agents, w["name"], _WATCHER_STALE_SECONDS)
        if not hb_status["has_run"]:
            status = "never ticked"
        elif hb_status["stale"]:
            status = "stale"
        else:
            status = "active"
        watchers.append({**w, "status": status, "last_heartbeat": hb_status["last_heartbeat"]})

    return get_templates().TemplateResponse(request, "schedules.html", {
        "schedules": schedules,
        "watchers": watchers,
        "apps": fleet,
    })


@router.post("/schedules/update", response_model=None)
async def update_schedule(request: Request):
    """No longer reachable from any UI control (see ``schedules_page()``
    above/schedules.html) -- this used to look like it edited a live
    CronJob's real schedule, but only ever wrote a display-only settings
    key nothing (not even this page anymore) reads back. Kept only for
    ``AGENTIT_DB_DSN``-level introspection/tests; a real schedule change
    goes through editing the generated manifest + Dry Run + Apply/Commit
    (or a GitOps PR) on that app's Onboarding Results page instead.
    """
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    job_key = str(form.get("job_key", ""))
    schedule = str(form.get("schedule", "")).strip()
    if not (app_name and job_key and schedule):
        return RedirectResponse(url="/schedules?error=Missing+required+fields", status_code=303)
    if len(schedule.split()) != 5:
        return RedirectResponse(url="/schedules?error=Invalid+cron+expression", status_code=303)
    s = await get_store()
    await s.set_setting(f"schedule:{app_name}:{job_key}", schedule)
    await s.log_event(
        "portal", "schedule-updated", app_name, "info",
        f"Schedule for {job_key} updated to: {schedule}",
    )
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/toggle", response_model=None)
async def toggle_schedule(request: Request):
    """No longer reachable from any UI control -- see ``update_schedule()``
    above for why (same display-only settings key, same lack of any real
    effect on a live CronJob)."""
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    job_key = str(form.get("job_key", ""))
    enabled = str(form.get("enabled", "true"))
    if not (app_name and job_key):
        return RedirectResponse(url="/schedules?error=Missing+required+fields", status_code=303)
    s = await get_store()
    await s.set_setting(f"schedule:{app_name}:{job_key}:enabled", enabled)
    action = "enabled" if enabled == "true" else "disabled"
    await s.log_event(
        "portal", f"schedule-{action}", app_name, "info",
        f"Schedule {job_key} {action} for {app_name}",
    )
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/create", response_model=None)
async def create_schedule(request: Request):
    form = await request.form()
    app_name = str(form.get("app_name", "")).strip()
    job_name = str(form.get("job_name", "")).strip()
    agent = str(form.get("agent", "")).strip()
    schedule = str(form.get("schedule", "")).strip()
    command = str(form.get("command", "")).strip()

    if not all([app_name, job_name, agent, schedule, command]):
        raise HTTPException(400, "All fields are required: app_name, job_name, agent, schedule, command")
    if len(schedule.split()) != 5:
        raise HTTPException(400, "Invalid cron expression: must have exactly 5 fields")

    s = await get_store()
    await s.create_schedule(app_name, job_name, agent, schedule, command)
    await s.log_event(
        "portal", "schedule-created", app_name, "info",
        f"Manual schedule created: {job_name} ({schedule})",
    )
    return RedirectResponse(url="/schedules?created=true", status_code=303)


@router.post("/schedules/delete", response_model=None)
async def delete_schedule_route(request: Request):
    form = await request.form()
    schedule_id = str(form.get("schedule_id", "")).strip()
    if not schedule_id:
        raise HTTPException(400, "schedule_id required")

    s = await get_store()
    if not await s.delete_schedule(schedule_id):
        raise HTTPException(404, "Schedule not found")
    await s.log_event("portal", "schedule-deleted", None, "info", f"Deleted manual schedule {schedule_id}")
    return RedirectResponse(url="/schedules?deleted=true", status_code=303)
