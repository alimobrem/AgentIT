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


def instrument_app(app):
    """Attach Prometheus instrumentation to the FastAPI app."""
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/healthz", "/readyz", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics")
