"""Collect live SLO metrics from the cluster via oc/kubectl."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _find_cli() -> str | None:
    for cmd in ("oc", "kubectl"):
        if shutil.which(cmd):
            return cmd
    return None


def collect_slo(metric_name: str, namespace: str) -> float | None:
    """Collect a single SLO metric value from the cluster.

    Returns the metric value as a float, or None if collection fails.
    """
    cli = _find_cli()
    if cli is None:
        logger.warning("Neither oc nor kubectl found on PATH, cannot collect SLO")
        return None

    collectors = {
        "error_rate": _collect_error_rate,
        "availability": _collect_availability,
    }
    fn = collectors.get(metric_name)
    if fn is None:
        logger.debug("No collector for metric %r, skipping", metric_name)
        return None
    return fn(cli, namespace)


def _collect_error_rate(cli: str, namespace: str) -> float | None:
    """Error rate = non-ready pods / total pods (as percentage)."""
    try:
        result = subprocess.run(
            [cli, "get", "pods", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Failed to get pods for error_rate: %s", result.stderr.strip())
            return None
        data = json.loads(result.stdout)
        items = data.get("items", [])
        if not items:
            return 0.0
        non_ready = 0
        for pod in items:
            statuses = (pod.get("status") or {}).get("containerStatuses") or []
            if any(not cs.get("ready", False) for cs in statuses):
                non_ready += 1
        return (non_ready / len(items)) * 100.0
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("error_rate collection failed: %s", exc)
        return None


def _collect_availability(cli: str, namespace: str) -> float | None:
    """Availability = running pods / total pods (as percentage)."""
    try:
        total_result = subprocess.run(
            [cli, "get", "pods", "-n", namespace, "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if total_result.returncode != 0:
            logger.warning("Failed to get pods for availability: %s", total_result.stderr.strip())
            return None
        data = json.loads(total_result.stdout)
        total = len(data.get("items", []))
        if total == 0:
            return 100.0
        running_result = subprocess.run(
            [cli, "get", "pods", "-n", namespace,
             "--field-selector=status.phase=Running", "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
        if running_result.returncode != 0:
            logger.warning("Failed to get running pods: %s", running_result.stderr.strip())
            return None
        running_data = json.loads(running_result.stdout)
        running = len(running_data.get("items", []))
        return (running / total) * 100.0
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("availability collection failed: %s", exc)
        return None
