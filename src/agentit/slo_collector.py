"""Collect live SLO metrics from the cluster via the kubernetes Python client."""

from __future__ import annotations

import logging

from agentit import kube
from agentit.kube import KubeError

logger = logging.getLogger(__name__)

# Whether a *higher* current value is considered healthy for a given metric.
# availability: higher is better (more uptime). error_rate/latency: lower is
# better (fewer errors, faster responses). Unknown metrics default to
# "lower is better" to match the historical breach comparison.
HIGHER_IS_BETTER: dict[str, bool] = {
    "availability": True,
    "error_rate": False,
    "latency_p99_ms": False,
}


def is_breached(metric_name: str, current_value: float, target_value: float) -> bool:
    """Determine SLO breach status using the correct direction for the metric.

    For higher-is-better metrics (e.g. availability), a breach is a value
    *below* target. For lower-is-better metrics (e.g. error_rate,
    latency_p99_ms), a breach is a value *above* target.
    """
    if HIGHER_IS_BETTER.get(metric_name, False):
        return current_value < target_value
    return current_value > target_value


def collect_slo(metric_name: str, namespace: str) -> float | None:
    """Collect a single SLO metric value from the cluster.

    Returns the metric value as a float, or None if collection fails or if
    there is no collector implemented for this metric type (e.g.
    latency_p99_ms currently has no cluster-side collector).
    """
    collectors = {
        "error_rate": _collect_error_rate,
        "availability": _collect_availability,
    }
    fn = collectors.get(metric_name)
    if fn is None:
        logger.warning(
            "No collector implemented for metric %r (namespace=%s) — skipping collection",
            metric_name, namespace,
        )
        return None
    return fn(namespace)


def _collect_error_rate(namespace: str) -> float | None:
    """Error rate = non-ready pods / total pods (as percentage)."""
    try:
        pods = kube.list_pods(namespace)
    except KubeError as exc:
        logger.warning("Cannot collect error_rate: %s", exc)
        return None
    if not pods:
        return 0.0
    non_ready = sum(1 for p in pods if not p["ready"])
    return (non_ready / len(pods)) * 100.0


def _collect_availability(namespace: str) -> float | None:
    """Availability = running pods / total pods (as percentage)."""
    try:
        pods = kube.list_pods(namespace)
    except KubeError as exc:
        logger.warning("Cannot collect availability: %s", exc)
        return None
    if not pods:
        return 100.0
    running = sum(1 for p in pods if p["status"] == "Running")
    return (running / len(pods)) * 100.0
