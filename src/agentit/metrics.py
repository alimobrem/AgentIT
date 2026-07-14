"""Low-level Prometheus metric objects shared by orchestration and the portal.

Kept separate from ``portal/metrics.py`` (and imported lazily, never at
module import time, by ``agents/orchestrator.py``) so ``FleetOrchestrator``
-- used standalone by the CLI's ``onboard``/``orchestrate`` commands, a
first-class, portal-independent use case -- doesn't transitively import
``prometheus_client`` (nor, via ``portal/metrics.py``, the much heavier
``prometheus_fastapi_instrumentator``/FastAPI app graph) merely to run an
agent locally. ``portal/metrics.py`` re-exports these two for every
portal-side caller that already imports agent-run metrics from there.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram

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
