"""Schedule management endpoints."""
from __future__ import annotations

import asyncio
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

_WATCHER_AGENTS = [
    {"name": "vuln-watcher", "mode": "Kafka consumer + polling", "interval": "6 hours"},
    {"name": "slo-tracker", "mode": "Polling", "interval": "5 minutes"},
    {"name": "drift-detector", "mode": "Argo CD polling", "interval": "10 minutes"},
]


def _get_watcher_deploy_status() -> dict[str, str]:
    from agentit import kube

    result: dict[str, str] = {}
    try:
        deps = kube.apps_v1().list_namespaced_deployment(
            "agentit", label_selector="app.kubernetes.io/instance=agentit",
        )
        for dep in deps.items:
            name = dep.metadata.name
            ready = dep.status.ready_replicas or 0
            result[name] = "running" if ready > 0 else "not running"
    except Exception:
        log.debug("Failed to get deployment status", exc_info=True)
    return result


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request) -> HTMLResponse:
    import yaml as _yaml

    s = get_store()
    fleet = s.get_fleet_data()
    schedules: list[dict] = []

    for app_data in fleet:
        aid = app_data["id"]
        files = s.get_onboarding(aid)
        if not files:
            continue
        for f in files:
            sched_info = _SCHEDULE_FILES.get(f["path"])
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
            override_key = f"schedule:{app_data['repo_name']}:{agent}"
            override = s.get_setting(override_key)
            if override:
                cron = override
            enabled_key = f"schedule:{app_data['repo_name']}:{agent}:enabled"
            enabled_val = s.get_setting(enabled_key)
            enabled = enabled_val != "false"

            schedules.append({
                "app_name": app_data["repo_name"],
                "job_name": desc,
                "schedule": cron,
                "human_schedule": _CRON_HUMAN.get(cron, cron),
                "agent": agent,
                "concurrency": concurrency,
                "enabled": enabled,
            })

    # Merge manually created schedules from the store
    manual_schedules = s.list_schedules()
    for ms in manual_schedules:
        schedules.append({
            "id": ms["id"],
            "app_name": ms["app_name"],
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

    agents = s.list_agents()
    deploy_status = await asyncio.to_thread(_get_watcher_deploy_status)
    watchers = []
    for w in _WATCHER_AGENTS:
        agent_record = next((a for a in agents if a["agent_name"] == w["name"]), None)
        deploy_name = f"agentit-{w['name']}"
        if agent_record:
            status = agent_record["status"]
        elif deploy_status.get(deploy_name) == "running":
            status = "active"
        else:
            status = "not deployed"
        watchers.append({**w, "status": status})

    return get_templates().TemplateResponse(request, "schedules.html", {
        "schedules": schedules,
        "watchers": watchers,
        "apps": fleet,
    })


@router.post("/schedules/update", response_model=None)
async def update_schedule(request: Request):
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    job_key = str(form.get("job_key", ""))
    schedule = str(form.get("schedule", "")).strip()
    if app_name and job_key and schedule:
        if len(schedule.split()) != 5:
            raise HTTPException(400, "Invalid cron expression: must have exactly 5 fields")
        get_store().set_setting(f"schedule:{app_name}:{job_key}", schedule)
        get_store().log_event(
            "portal", "schedule-updated", app_name, "info",
            f"Schedule for {job_key} updated to: {schedule}",
        )
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/toggle", response_model=None)
async def toggle_schedule(request: Request):
    form = await request.form()
    app_name = str(form.get("app_name", ""))
    job_key = str(form.get("job_key", ""))
    enabled = str(form.get("enabled", "true"))
    if app_name and job_key:
        get_store().set_setting(f"schedule:{app_name}:{job_key}:enabled", enabled)
        action = "enabled" if enabled == "true" else "disabled"
        get_store().log_event(
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

    s = get_store()
    s.create_schedule(app_name, job_name, agent, schedule, command)
    s.log_event(
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

    s = get_store()
    if not s.delete_schedule(schedule_id):
        raise HTTPException(404, "Schedule not found")
    s.log_event("portal", "schedule-deleted", None, "info", f"Deleted manual schedule {schedule_id}")
    return RedirectResponse(url="/schedules?deleted=true", status_code=303)
