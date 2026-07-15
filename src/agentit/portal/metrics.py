"""Prometheus metrics for AgentIT self-monitoring."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info
from prometheus_fastapi_instrumentator import Instrumentator

# agent_runs_total / agent_run_duration_seconds live in the top-level
# agentit.metrics module (not here) so agents/orchestrator.py -- used
# standalone by the CLI's onboard/orchestrate commands -- can lazily
# import them without dragging in this module's prometheus_fastapi_
# instrumentator/FastAPI dependency. Re-exported here for portal-side
# callers that already import agent-run metrics from portal.metrics.
from agentit.metrics import agent_runs_total, agent_run_duration_seconds

assessments_total = Counter(
    "agentit_assessments_total",
    "Total assessments run",
    ["criticality", "status"],
)

onboardings_total = Counter(
    "agentit_onboardings_total",
    "Total onboarding runs",
    ["status"],
)

remediations_total = Counter(
    "agentit_remediations_total",
    "Total remediations dispatched",
    ["agent", "status"],
)

active_gates = Gauge(
    "agentit_active_gates",
    "Number of pending approval gates",
)

fleet_size = Gauge(
    "agentit_fleet_size",
    "Number of managed applications",
)

fleet_avg_score = Gauge(
    "agentit_fleet_avg_score",
    "Average enterprise readiness score across fleet",
)

build_info = Info(
    "agentit_build",
    "AgentIT build information",
)

# In-process cache of the last values passed to `build_info.info(...)` --
# `prometheus_client.Info` doesn't expose a public read-back API, and the
# ambient deploy-status indicator (routes/health.py) needs "what version is
# this instance actually running" on every request without scraping our own
# /metrics endpoint over HTTP.
_current_build_info: dict[str, str] = {"version": "unknown", "commit": "unknown", "image_tag": "unknown"}


def set_build_info(version: str, commit: str, image_tag: str) -> None:
    """Populate the `agentit_build` Info metric and cache the same values for
    `get_build_info()`. Called once at startup (`app.py`'s
    `_set_build_info`)."""
    global _current_build_info
    _current_build_info = {"version": version, "commit": commit, "image_tag": image_tag}
    build_info.info(_current_build_info)


def get_build_info() -> dict[str, str]:
    """Return the build info last set by `set_build_info()` -- the
    "currently running" half of the deploy-status indicator."""
    return dict(_current_build_info)

circuit_breaker_open = Gauge(
    "agentit_circuit_breaker_open",
    "1 if the named circuit breaker is currently open (failing closed), else 0",
    ["name"],
)

event_buffer_backlog = Gauge(
    "agentit_event_buffer_backlog",
    "Number of events buffered locally in event-buffer.db, pending Kafka delivery",
)

db_size_bytes = Gauge(
    "agentit_db_size_bytes",
    "On-disk size of the portal's SQLite database file",
)

db_rows_total = Gauge(
    "agentit_db_rows_total",
    "Row count per table in the portal's SQLite database",
    ["table"],
)

watcher_last_success_timestamp = Gauge(
    "agentit_watcher_last_success_timestamp",
    "Unix timestamp of the last successful tick for each long-lived watcher",
    ["watcher"],
)

# ── Self-monitoring additions (synthetic probe, backup, secret checks) ──
#
# These are all reported via internal webhooks (verify_internal_token-gated,
# same convention as every other /api/webhook/* route) from CronJobs that run
# outside this process -- see chart/templates/synthetic-probe-cronjob.yaml,
# workflows/db-backup-cronjob.yaml, and secret-rotation-cronjob.yaml. Every
# gauge here has a matching *_last_run_timestamp so a PrometheusRule can
# alert on staleness (the CronJob itself stopped running/succeeding), not
# just on the last-reported value -- same pattern as
# agentit_watcher_last_success_timestamp above.

synthetic_probe_up = Gauge(
    "agentit_synthetic_probe_up",
    "1 if the last external synthetic probe against the public Route succeeded, else 0",
)

synthetic_probe_last_run_timestamp = Gauge(
    "agentit_synthetic_probe_last_run_timestamp",
    "Unix timestamp of the last synthetic probe report received",
)

route_cert_expiry_days = Gauge(
    "agentit_route_cert_expiry_days",
    "Days remaining until the public Route's TLS certificate expires, as observed by the synthetic probe",
)

backup_last_status = Gauge(
    "agentit_backup_last_status",
    "1 if the last backup job for this target succeeded, else 0",
    ["target"],
)

backup_last_success_timestamp = Gauge(
    "agentit_backup_last_success_timestamp",
    "Unix timestamp of the last successful backup for this target",
    ["target"],
)

secret_check_status = Gauge(
    "agentit_secret_check_status",
    "1 if the named secret last passed its existence/drift check, else 0",
    ["secret"],
)

secret_check_last_run_timestamp = Gauge(
    "agentit_secret_check_last_run_timestamp",
    "Unix timestamp of the last secret-check report received",
    ["secret"],
)


def refresh_circuit_breaker_gauge() -> None:
    """Set `agentit_circuit_breaker_open` from the live breaker states."""
    from agentit.portal.helpers import get_circuit_breaker_states
    for name, state in get_circuit_breaker_states().items():
        circuit_breaker_open.labels(name=name).set(1 if state["open"] else 0)


def refresh_db_metrics(store) -> None:
    """Set DB size/row-count and event-buffer-backlog gauges from a store instance."""
    stats = store.get_db_stats()
    db_size_bytes.set(stats["size_bytes"])
    for table, count in stats["row_counts"].items():
        db_rows_total.labels(table=table).set(count)

    from agentit.events import get_publisher
    event_buffer_backlog.set(get_publisher().get_buffer_backlog())


def instrument_app(app):
    """Attach Prometheus instrumentation to the FastAPI app."""
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/healthz", "/readyz", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics")
