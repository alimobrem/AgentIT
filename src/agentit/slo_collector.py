"""Collect live SLO metrics from the cluster via the kubernetes Python client."""

from __future__ import annotations

import logging

from agentit import kube
from agentit.kube import KubeError

logger = logging.getLogger(__name__)


def collect_slo(metric_name: str, namespace: str) -> float | None:
    """Collect a single SLO metric value from the cluster.

    Returns the metric value as a float, or None if collection fails.
    """
    collectors = {
        "error_rate": _collect_error_rate,
        "availability": _collect_availability,
    }
    fn = collectors.get(metric_name)
    if fn is None:
        logger.debug("No collector for metric %r, skipping", metric_name)
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
