"""Live-cluster evidence for finding-clear (quota / scaling / health).

Fleet remediations land in gitops and are applied by Argo. Source-only
analyzers never see those manifests, so a merged ResourceQuota/HPA would
leave the finding open forever. When the app namespace is known, treat
live ResourceQuota/LimitRange/HPA as clearing evidence — the same truth
``correlate_delivery_finding`` needs after merge + sync.

``health`` (ha_dr.py's "No liveness or readiness probes defined") gets the
same treatment for a stronger reason than quota/scaling: unlike a
ResourceQuota/HPA (standalone additive resources AgentIT can safely
generate into gitops), `livenessProbe`/`readinessProbe` are a patch to an
*existing* container spec AgentIT does not own the base definition of --
see skills/infrastructure/health-probes-policy.md's docstring for the full
"why not generate a Deployment patch" reasoning. That skill instead
generates a namespace-scoped Kyverno mutate policy, which fixes the live
workload's containers without AgentIT ever touching the app's source repo
or gitops Deployment file -- exactly the shape of fix a source-only
analyzer would otherwise never learn about. ``live_health_probes_present``
closes that loop.
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


def _hpa_scale_target_resolves(namespace: str, hpa: dict) -> bool:
    """True when the HPA's scaleTargetRef names a live Deployment or Rollout.

    A leftover HPA pointing at a deleted/wrong workload must not clear
    ``scaling`` — dogfood pinky had Deployment/pinky while only Rollout/pinky
    existed, AbleToScale=False, yet presence-only clearing hid the finding.
    """
    ref = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
    kind = (ref.get("kind") or "").strip()
    name = (ref.get("name") or "").strip()
    if not kind or not name:
        return False
    try:
        from agentit import kube

        if kind == "Deployment":
            return kube.apps_v1().read_namespaced_deployment(
                name, namespace, _request_timeout=10,
            ) is not None
        if kind == "Rollout":
            obj = kube.get_custom_resource(
                "argoproj.io", "v1alpha1", "rollouts", name, namespace=namespace,
            )
            return obj is not None
    except Exception as exc:
        logger.debug(
            "HPA scaleTargetRef check failed for %s/%s %s/%s: %s",
            namespace, hpa.get("metadata", {}).get("name"), kind, name, exc,
        )
        return False
    # ReplicaSet/StatefulSet/etc. — treat as unresolved for clearing purposes
    return False


def live_hpa_present(namespace: str) -> bool | None:
    """True when a *working* HorizontalPodAutoscaler exists in *namespace*.

    "Working" means scaleTargetRef resolves to a live Deployment or Rollout.
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
                if items is None:
                    continue
                if any(_hpa_scale_target_resolves(namespace, h) for h in items):
                    return True
                if items:
                    # HPAs exist but none resolve — do not clear scaling
                    return False
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


def _deployment_containers_have_probes(containers: list) -> bool:
    """``containers``: ``list[V1Container]`` (typed kubernetes-client objects,
    snake_case attributes) from a live ``Deployment``. Empty containers is
    treated as "no signal" (False, not vacuously True) -- a Deployment with
    no readable container spec should never look like a passing probe check.
    """
    if not containers:
        return False
    return all(bool(c.liveness_probe) and bool(c.readiness_probe) for c in containers)


def _rollout_containers_have_probes(containers: list[dict]) -> bool:
    """``containers``: raw dicts (camelCase keys) from a live ``Rollout``
    custom resource -- ``list_custom_resources`` never returns typed
    objects, only plain dicts."""
    if not containers:
        return False
    return all(
        isinstance(c, dict) and bool(c.get("livenessProbe")) and bool(c.get("readinessProbe"))
        for c in containers
    )


def live_health_probes_present(namespace: str) -> bool | None:
    """True when every container of every live Deployment/Rollout in
    *namespace* already has both ``livenessProbe`` and ``readinessProbe``
    configured -- i.e. a real fix (GitOps-delivered Deployment edit,
    ``skills/infrastructure/health-probes-policy.md``'s Kyverno mutate
    policy, or a hand-applied change) has already landed on the *live*
    workload, even though the app's source repo (all ``ha_dr.py`` can see)
    never mentions a probe anywhere.

    Mirrors ``live_quota_present``/``live_hpa_present``'s tri-state
    contract:

    - ``True``: at least one Deployment/Rollout was found and every one of
      their containers has both probes configured.
    - ``False``: at least one Deployment/Rollout was found but at least one
      container is missing a probe -- the finding legitimately still holds.
    - ``None``: no signal at all (no Deployment/Rollout found yet, or
      discovery failed) -- do not clear. An app with nothing running yet
      should keep the source-repo finding, not silently look "fixed".
    """
    if not namespace:
        return None
    try:
        from agentit import kube

        found_any = False
        all_have_probes = True

        try:
            deployments = kube.apps_v1().list_namespaced_deployment(
                namespace, _request_timeout=10,
            )
            for dep in deployments.items:
                spec = dep.spec if dep.spec else None
                template_spec = spec.template.spec if spec and spec.template else None
                containers = template_spec.containers if template_spec else []
                found_any = True
                if not _deployment_containers_have_probes(containers):
                    all_have_probes = False
        except Exception as exc:
            logger.debug("live Deployment probe discovery failed for %s: %s", namespace, exc)
            return None

        try:
            rollouts = kube.list_custom_resources(
                "argoproj.io", "v1alpha1", "rollouts", namespace=namespace, timeout=10,
            )
            for ro in rollouts:
                ro_spec = ro.get("spec") or {}
                containers = ((ro_spec.get("template") or {}).get("spec") or {}).get(
                    "containers", [],
                )
                found_any = True
                if not _rollout_containers_have_probes(containers):
                    all_have_probes = False
        except Exception as exc:
            # Rollout CRD may legitimately be absent -- Deployments alone
            # are enough (mirrors fleet_hpa.discover_namespace_workloads'
            # own posture of tolerating a missing Rollout CRD).
            logger.debug("Rollouts unavailable in %s: %s", namespace, exc)

        if not found_any:
            return None
        return all_have_probes
    except Exception as exc:
        logger.debug("live health-probe discovery failed for %s: %s", namespace, exc)
        return None


def _drop_categories(findings: list[Finding], categories: set[str]) -> list[Finding]:
    return [f for f in findings if f.category not in categories]


def apply_live_cluster_finding_clear(
    scores: list[DimensionScore],
    repo_name: str,
) -> list[DimensionScore]:
    """Drop quota/scaling/health findings cleared by live cluster evidence.

    Fail-open on discovery errors (returns scores unchanged for that
    category) so a kube blip never fabricates a clean bill of health.
    """
    ns = namespace_for_repo(repo_name)
    drop: set[str] = set()
    if live_quota_present(ns) is True:
        drop.add("quota")
    if live_hpa_present(ns) is True:
        drop.add("scaling")
    if live_health_probes_present(ns) is True:
        drop.add("health")
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
