"""Adaptive resource tuning based on observed usage patterns."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("AGENTIT_PROMETHEUS_URL", "http://prometheus-k8s.openshift-monitoring.svc:9090")


@dataclass
class ResourceRecommendation:
    resource_type: str
    current_value: str
    recommended_value: str
    reason: str
    confidence: float


def _sanitize_prom_label(value: str) -> str:
    """Strip non-safe characters for PromQL label values."""
    return re.sub(r'[^a-zA-Z0-9_.\-]', '', value)


def query_prometheus(query: str, timeout: int = 10) -> float | None:
    try:
        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results and results[0].get("value"):
            return float(results[0]["value"][1])
    except Exception as exc:
        logger.debug("Prometheus query failed: %s", exc)
    return None


def analyze_resource_usage(app_name: str, namespace: str) -> list[ResourceRecommendation]:
    recommendations: list[ResourceRecommendation] = []
    app_name = _sanitize_prom_label(app_name)
    namespace = _sanitize_prom_label(namespace)

    avg_cpu = query_prometheus(
        f'avg(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod=~"{app_name}.*"}}[7d]))'
    )
    cpu_request = query_prometheus(
        f'avg(kube_pod_container_resource_requests{{namespace="{namespace}",pod=~"{app_name}.*",resource="cpu"}})'
    )

    if avg_cpu is not None and cpu_request is not None and cpu_request > 0:
        utilization = avg_cpu / cpu_request
        if utilization < 0.2:
            new_req = max(0.05, avg_cpu * 1.5)
            recommendations.append(ResourceRecommendation(
                resource_type="cpu_request",
                current_value=f"{cpu_request*1000:.0f}m",
                recommended_value=f"{new_req*1000:.0f}m",
                reason=f"CPU utilization is {utilization:.0%} -- over-provisioned",
                confidence=0.8,
            ))
        elif utilization > 0.8:
            max_cpu = query_prometheus(
                f'max(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod=~"{app_name}.*"}}[7d]))'
            )
            new_req = (max_cpu or avg_cpu) * 1.3
            recommendations.append(ResourceRecommendation(
                resource_type="cpu_request",
                current_value=f"{cpu_request*1000:.0f}m",
                recommended_value=f"{new_req*1000:.0f}m",
                reason=f"CPU utilization is {utilization:.0%} -- risk of throttling",
                confidence=0.7,
            ))

    avg_mem = query_prometheus(
        f'avg(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{app_name}.*"}})'
    )
    mem_request = query_prometheus(
        f'avg(kube_pod_container_resource_requests{{namespace="{namespace}",pod=~"{app_name}.*",resource="memory"}})'
    )

    if avg_mem is not None and mem_request is not None and mem_request > 0:
        utilization = avg_mem / mem_request
        if utilization < 0.3:
            max_mem = query_prometheus(
                f'max(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{app_name}.*"}})'
            )
            new_req = max(64 * 1024 * 1024, (max_mem or avg_mem) * 1.3)
            recommendations.append(ResourceRecommendation(
                resource_type="memory_request",
                current_value=f"{mem_request/1024/1024:.0f}Mi",
                recommended_value=f"{new_req/1024/1024:.0f}Mi",
                reason=f"Memory utilization is {utilization:.0%} -- over-provisioned",
                confidence=0.8,
            ))
        elif utilization > 0.85:
            max_mem = query_prometheus(
                f'max(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{app_name}.*"}})'
            )
            new_req = (max_mem or avg_mem) * 1.3
            recommendations.append(ResourceRecommendation(
                resource_type="memory_request",
                current_value=f"{mem_request/1024/1024:.0f}Mi",
                recommended_value=f"{new_req/1024/1024:.0f}Mi",
                reason=f"Memory utilization is {utilization:.0%} -- risk of OOM",
                confidence=0.9,
            ))

    return recommendations
