"""Prometheus metrics for AgentIT self-monitoring."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info
from prometheus_fastapi_instrumentator import Instrumentator

assessments_total = Counter(
    "agentit_assessments_total",
    "Total assessments run",
    ["criticality", "status"],
)

agent_runs_total = Counter(
    "agentit_agent_runs_total",
    "Total agent executions",
    ["agent", "mode", "status"],
)

agent_run_duration_seconds = Histogram(
    "agentit_agent_run_duration_seconds",
    "Agent execution duration",
    ["agent", "mode"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300],
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
