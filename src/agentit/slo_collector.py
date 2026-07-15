"""Collect live SLO metrics from the cluster via the kubernetes Python client
(availability/error_rate, from pod status) and Prometheus (latency_p99_ms,
which has no pod-status equivalent and needs the app's own request-duration
histogram).
"""

from __future__ import annotations

import logging
import math

from agentit import kube
from agentit.kube import KubeError
from agentit.resource_tuner import _sanitize_prom_label, query_prometheus

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

    Returns the metric value as a float, or None if collection fails, if
    there is no collector implemented for this metric type, or if the
    collector ran but found no data yet (e.g. a brand-new app with no
    traffic yet for latency_p99_ms).
    """
    collectors = {
        "error_rate": _collect_error_rate,
        "availability": _collect_availability,
        "latency_p99_ms": _collect_latency_p99,
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


def _collect_latency_p99(namespace: str) -> float | None:
    """Latency p99 in milliseconds, via the same in-cluster Prometheus this
    project already queries for resource tuning (``resource_tuner.query_prometheus``,
    ``AGENTIT_PROMETHEUS_URL``). Pod status alone (the source for the other two
    collectors) can't yield a request-latency percentile, so this scopes a
    histogram_quantile query to the app's own namespace -- the same per-app
    scoping label (``namespace``) the other collectors implicitly use via
    ``kube.list_pods(namespace)``.

    Returns None (never a fabricated value) if Prometheus is unreachable or
    if the app genuinely has no request-duration samples yet (e.g. a
    brand-new app with zero traffic) -- Prometheus reports that as an empty
    or NaN result, not an error.
    """
    safe_namespace = _sanitize_prom_label(namespace)
    seconds = query_prometheus(
        "histogram_quantile(0.99, "
        f'sum(rate(http_request_duration_seconds_bucket{{namespace="{safe_namespace}"}}[5m])) by (le))'
    )
    if seconds is None or math.isnan(seconds):
        logger.warning(
            "No latency_p99_ms data available from Prometheus for namespace=%s "
            "(no request-duration samples yet, e.g. a new app with no traffic) -- skipping collection",
            namespace,
        )
        return None
    return seconds * 1000.0
