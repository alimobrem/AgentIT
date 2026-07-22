"""Live-cluster evidence for finding-clear (quota / scaling).

Fleet remediations land in gitops and are applied by Argo. Source-only
analyzers never see those manifests, so a merged ResourceQuota/HPA would
leave the finding open forever. When the app namespace is known, treat
live ResourceQuota/LimitRange/HPA as clearing evidence — the same truth
``correlate_delivery_finding`` needs after merge + sync.
"""
from __future__ import annotations

import logging

from agentit.models import DimensionScore, Finding

logger = logging.getLogger(__name__)


def namespace_for_repo(repo_name: str) -> str:
    """Match delivery's app-namespace convention (routes/assessments.py)."""
    return (repo_name or "").lower().replace("_", "-").replace(".", "-").strip("-") or "default"


def live_quota_present(namespace: str) -> bool | None:
    """True when ResourceQuota or LimitRange exists in *namespace*.

    Returns ``None`` when discovery fails (no signal — do not clear).
    """
    if not namespace:
        return None
    try:
        from agentit import kube

        quotas = kube.core_v1().list_namespaced_resource_quota(
            namespace, _request_timeout=10,
        )
        if quotas.items:
            return True
        limits = kube.core_v1().list_namespaced_limit_range(
            namespace, _request_timeout=10,
        )
        return bool(limits.items)
    except Exception as exc:
        logger.debug("live quota discovery failed for %s: %s", namespace, exc)
        return None


def live_hpa_present(namespace: str) -> bool | None:
    """True when any HorizontalPodAutoscaler exists in *namespace*.

    Returns ``None`` when discovery fails (no signal — do not clear).
    """
    if not namespace:
        return None
    try:
        from agentit import kube

        # Use the dynamic/custom-objects path (same as fleet_hpa Rollouts) —
        # typed AutoscalingV2Api hits client_side_validation AttributeError
        # with some kubernetes-client + OpenShift combinations.
        for version in ("v2", "v1"):
            try:
                items = kube.list_custom_resources(
                    "autoscaling", version, "horizontalpodautoscalers",
                    namespace=namespace, timeout=10,
                )
                if items:
                    return True
            except Exception as inner:
                logger.debug(
                    "HPA list autoscaling/%s failed for %s: %s",
                    version, namespace, inner,
                )
        # Empty successful lists → definitively absent
        return False
    except Exception as exc:
        logger.debug("live HPA discovery failed for %s: %s", namespace, exc)
        return None


def _drop_categories(findings: list[Finding], categories: set[str]) -> list[Finding]:
    return [f for f in findings if f.category not in categories]


def apply_live_cluster_finding_clear(
    scores: list[DimensionScore],
    repo_name: str,
) -> list[DimensionScore]:
    """Drop quota/scaling findings cleared by live cluster evidence.

    Fail-open on discovery errors (returns scores unchanged for that
    category) so a kube blip never fabricates a clean bill of health.
    """
    ns = namespace_for_repo(repo_name)
    drop: set[str] = set()
    if live_quota_present(ns) is True:
        drop.add("quota")
    if live_hpa_present(ns) is True:
        drop.add("scaling")
    if not drop:
        return scores

    from agentit.analyzers.base import calculate_score

    updated: list[DimensionScore] = []
    for score in scores:
        kept = _drop_categories(score.findings, drop)
        if len(kept) == len(score.findings):
            updated.append(score)
            continue
        updated.append(DimensionScore(
            dimension=score.dimension,
            score=calculate_score(kept),
            max_score=score.max_score,
            findings=kept,
        ))
    logger.info(
        "Live cluster cleared findings %s for namespace %s",
        sorted(drop), ns,
    )
    return updated
