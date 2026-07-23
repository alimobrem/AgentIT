"""Schedule management endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


# ── Cron humanization ────────────────────────────────────────────────
#
# AgentIT's own skill templates (skills/compliance/compliance-cronjob.md,
# skills/cost/cost-cronjob.md, skills/dependency/dependency-cronjob.md --
# the only three that currently generate a CronJob/CronWorkflow) only ever
# emit two shapes: weekly-on-a-given-weekday-and-hour ("0 8 * * 1") and
# monthly-on-a-given-day-and-hour ("0 2 1 * *"). This used to be a 5-entry
# dict of exact strings seen in practice, which meant any cron outside
# that list (e.g. cost-cronjob.md's own "0 8 * * 1") fell back to
# returning the raw cron string as its own "human-readable" version --
# rendered twice, back-to-back, in schedules.html. humanize_cron() below
# is a real (if intentionally narrow) parser for standard 5-field cron
# that covers those two shapes plus daily/yearly for completeness, and
# returns None for anything it can't confidently describe so callers
# never echo the raw string back as if it were human-readable.

_DOW_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _parse_int_field(value: str, lo: int, hi: int) -> int | None:
    """Parse a cron field that must be a single literal integer in [lo, hi]."""
    if not value or not (value.isdigit() or (value.startswith("-") and value[1:].isdigit())):
        return None
    n = int(value)
    return n if lo <= n <= hi else None


def _parse_dow_list(value: str) -> list[int] | None:
    """Parse a day-of-week field: a single day or a comma-separated list.
    Accepts 0-7 (both 0 and 7 mean Sunday, matching standard cron)."""
    days = []
    for part in value.split(","):
        n = _parse_int_field(part, 0, 7)
        if n is None:
            return None
        days.append(0 if n == 7 else n)
    return days


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_time(hour: int, minute: int) -> str:
    period = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}{period}" if minute == 0 else f"{display_hour}:{minute:02d}{period}"


def humanize_cron(cron: str) -> str | None:
    """Translate a standard 5-field cron expression (minute hour
    day-of-month month day-of-week) into a short English description.

    Returns ``None`` when the expression is malformed or doesn't match one
    of the shapes handled below -- callers must treat that as "nothing
    meaningful to show", not fall back to the raw cron string.
    """
    if not cron or not isinstance(cron, str):
        return None
    fields = cron.split()
    if len(fields) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    minute = _parse_int_field(minute_f, 0, 59)
    hour = _parse_int_field(hour_f, 0, 23)
    if minute is None or hour is None:
        return None

    dom_is_wild = dom_f == "*"
    month_is_wild = month_f == "*"
    dow_is_wild = dow_f == "*"
    time_str = _format_time(hour, minute)

    # Weekly: one or more specific weekdays, every day-of-month/month.
    if not dow_is_wild and dom_is_wild and month_is_wild:
        days = _parse_dow_list(dow_f)
        if not days:
            return None
        day_names = "/".join(_DOW_NAMES[d] for d in days)
        return f"Weekly ({day_names} {time_str} UTC)"

    # Monthly: one specific day-of-month, every month, any weekday.
    if not dom_is_wild and month_is_wild and dow_is_wild:
        day = _parse_int_field(dom_f, 1, 31)
        if day is None:
            return None
        return f"Monthly ({_ordinal(day)}, {time_str} UTC)"

    # Daily: every day-of-month, every month, any weekday.
    if dom_is_wild and month_is_wild and dow_is_wild:
        return f"Daily ({time_str} UTC)"

    # Yearly: one specific day of one specific month, any weekday.
    if not dom_is_wild and not month_is_wild and dow_is_wild:
        day = _parse_int_field(dom_f, 1, 31)
        month = _parse_int_field(month_f, 1, 12)
        if day is None or month is None:
            return None
        return f"Yearly ({_MONTH_NAMES[month - 1]} {_ordinal(day)}, {time_str} UTC)"

    return None


# ── Constants ─────────────────────────────────────────────────────────

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

_CADENCE_LABELS = {
    "daily": "Every 24 hours",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "manual": "Manual only",
}


def _cron_fields_from_manifest(content: str) -> tuple[str, str]:
    """Extract ``(schedule, concurrencyPolicy)`` from an onboarding
    CronJob/CronWorkflow YAML.

    Skill templates commonly emit a multi-document bundle that leads with
    ServiceAccount/Role/RoleBinding and only later the CronJob. Plain
    ``yaml.safe_load`` returns the first document only, so
    ``spec.schedule`` looked missing and the page rendered every row as
    "unresolvable" even when a real cron was in a later document.
    """
    import yaml as _yaml

    if not content or not isinstance(content, str):
        return "unknown", "unknown"
    try:
        for doc in _yaml.safe_load_all(content):
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") not in ("CronJob", "CronWorkflow"):
                continue
            spec = doc.get("spec") or {}
            if not isinstance(spec, dict):
                continue
            schedule = spec.get("schedule")
            if not schedule:
                continue
            concurrency = spec.get("concurrencyPolicy", "Allow")
            return str(schedule), str(concurrency)
    except (_yaml.YAMLError, ValueError, TypeError, AttributeError):
        return "unknown", "unknown"
    return "unknown", "unknown"


def _list_platform_cronjobs() -> list[dict]:
    """Live CronJobs in AgentIT's namespace (chart maintenance + probes).

    Fail-soft: offline/hermetic tests and a missing kube API must not
    500 this page. When listing fails, return [] and let the template
    say the platform table is unavailable rather than inventing rows.
    """
    import os

    from agentit import kube

    namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
    try:
        rows = kube.list_cronjobs(namespace)
    except Exception as exc:  # noqa: BLE001 -- page must stay up offline
        log.warning("Schedules: could not list platform CronJobs in %s: %s", namespace, exc)
        return []
    out: list[dict] = []
    for row in rows:
        schedule = row.get("schedule") or "unknown"
        out.append({
            "name": row.get("name") or "",
            "schedule": schedule,
            "human_schedule": humanize_cron(schedule) if schedule != "unknown" else None,
            "suspended": bool(row.get("suspended")),
            "last_schedule_time": row.get("last_schedule_time"),
            "last_successful_time": row.get("last_successful_time"),
        })
    return out


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request) -> HTMLResponse:
    s = await get_store()
    fleet = await s.get_fleet_data()
    schedules: list[dict] = []
    # repo_name -> latest assessment id, used to link "App Name" to that
    # app's Assessment Detail page wherever a real assessment_id can be
    # resolved (every other page -- Fleet, Decisions -- already does this;
    # Schedules didn't).
    app_ids_by_name = {app_data["repo_name"]: app_data["id"] for app_data in fleet}

    # Per-app re-assessment cadence (apps.assessment_cadence) is the real
    # "does this app have a schedule?" answer for Fleet -- driven by
    # reassess-scheduler, not by onboarding *-cronjob.yaml rows. Surface
    # it here so an empty/unresolvable CronWorkflow table cannot imply
    # "nothing is scheduled" when every app is on daily/weekly/monthly.
    app_cadences: list[dict] = []
    for app_data in fleet:
        cadence = await s.get_assessment_cadence(app_data["repo_url"])
        app_cadences.append({
            "app_name": app_data["repo_name"],
            "app_id": app_data["id"],
            "cadence": cadence,
            "cadence_label": _CADENCE_LABELS.get(cadence, cadence),
        })

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
            cron, concurrency = _cron_fields_from_manifest(f.get("content") or "")
            if cron != "unknown" and concurrency == "unknown":
                concurrency = "Allow"
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
                "human_schedule": humanize_cron(cron),
                "agent": agent,
                # None → template "n/a" when the CronJob doc itself was
                # never found in a multi-doc bundle (unresolvable cron).
                "concurrency": None if cron == "unknown" else concurrency,
                "enabled": True,
            })

    # Merge manually created reminders from the store -- see create_schedule()
    # below for why these are reminders, not real schedules: nothing ever
    # reads scheduled_operations to generate, apply, or deliver a CronJob, so
    # there is no real concurrencyPolicy to report either. `concurrency: None`
    # (rendered as "n/a" by schedules.html) instead of fabricating "Allow",
    # which used to claim a real Kubernetes semantic for an object that never
    # existed, even in principle.
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
            "human_schedule": humanize_cron(ms["schedule"]),
            "agent": ms["agent"],
            "concurrency": None,
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

    reassess_hb = watcher_heartbeat_status(agents, "reassess-scheduler", _WATCHER_STALE_SECONDS)
    reassess_scheduler_active = bool(reassess_hb["has_run"]) and not reassess_hb["stale"]

    platform_cronjobs = _list_platform_cronjobs()

    return get_templates().TemplateResponse(request, "schedules.html", {
        "schedules": schedules,
        "watchers": watchers,
        "apps": fleet,
        "app_cadences": app_cadences,
        "reassess_scheduler_active": reassess_scheduler_active,
        "platform_cronjobs": platform_cronjobs,
        "watchers_active_count": sum(1 for w in watchers if w["status"] == "active"),
    })


@router.post("/schedules/create", response_model=None)
async def create_schedule(request: Request):
    """Saves a plain reminder row in ``scheduled_operations`` -- nothing
    more. Unlike the onboarding-generated schedules above (real CronJob/
    CronWorkflow manifests, once actually delivered), this route never
    generates a manifest, never touches a cluster or GitOps repo, and the
    ``command`` a human types in is stored purely as a note-to-self -- it is
    never parsed, executed, or built into anything. There was never a real
    object behind a row this creates, even in principle, until a human
    separately writes one by hand (or onboards the app so AgentIT generates
    a real CronJob) outside this form. schedules.html's copy and the
    "Track a Schedule"/"Save Reminder" wording reflect this; see
    schedules_page() above for why manual rows report `concurrency: None`
    ("n/a") instead of a fabricated policy.
    """
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
        f"Schedule reminder saved: {job_name} ({schedule}) -- not a real CronJob; "
        "nothing was generated, applied, or delivered.",
    )
    return RedirectResponse(url="/schedules?created=true", status_code=303)


@router.post("/schedules/delete", response_model=None)
async def delete_schedule_route(request: Request):
    """Symmetric counterpart to create_schedule() above: deletes the same
    DB-only reminder row. This was already honest before this file's other
    changes -- it never claimed to remove a real CronJob either -- so its
    behavior is unchanged; only the event message below now says "reminder"
    to match create_schedule()'s wording.
    """
    form = await request.form()
    schedule_id = str(form.get("schedule_id", "")).strip()
    if not schedule_id:
        raise HTTPException(400, "schedule_id required")

    s = await get_store()
    if not await s.delete_schedule(schedule_id):
        raise HTTPException(404, "Schedule not found")
    await s.log_event("portal", "schedule-deleted", None, "info", f"Deleted schedule reminder {schedule_id}")
    return RedirectResponse(url="/schedules?deleted=true", status_code=303)
