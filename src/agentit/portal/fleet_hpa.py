"""Fleet-app HPA correctness: discover real workloads, refuse invented names.

Self-managed AgentIT uses ``self_managed_hpa`` (Helm / Rollout /
``{{ .Release.Name }}``). Fleet apps under ``apps/{app}/`` are different:
scale targets must match **live** Deployments or Rollouts in the app
namespace. Inventing ``Deployment/{app_name}`` when the real workloads are
``{app}-api`` / ``{app}-web`` / ``Rollout/{app}`` produces mergeable-looking
junk (pinky gitops #18) — refuse fail-closed rather than open that PR.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

# Prefer these Deployment name suffixes when there is no exact app_name match.
_PREFERRED_DEPLOY_SUFFIXES = (
    "-api",
    "-web",
    "-worker",
    "-frontend",
    "-backend",
    "-app",
    "-server",
    "-service",
)

# Skip infra/sidecars when expanding ``{app}-*`` Deployments.
_INFRA_DEPLOY_SUFFIXES = (
    "-temporal",
    "-temporal-ui",
    "-postgres",
    "-postgresql",
    "-mysql",
    "-mariadb",
    "-mongo",
    "-mongodb",
    "-redis",
    "-kafka",
    "-zookeeper",
    "-elasticsearch",
    "-opensearch",
    "-minio",
    "-rabbitmq",
    "-nats",
)


@dataclass(frozen=True)
class WorkloadRef:
    """A scaleable workload discovered in a namespace."""

    kind: str  # Deployment | Rollout
    name: str
    api_version: str

    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)


@dataclass(frozen=True)
class NamespaceWorkloads:
    """Deployments + Rollouts present in one namespace."""

    namespace: str
    deployments: tuple[str, ...]
    rollouts: tuple[str, ...]
    discovery_ok: bool = True

    def has(self, kind: str, name: str) -> bool:
        if kind == "Deployment":
            return name in self.deployments
        if kind == "Rollout":
            return name in self.rollouts
        return False

    def preferred_scale_targets(self, app_name: str) -> list[WorkloadRef]:
        """Best HPA targets for ``app_name`` — never invent missing names.

        Order:
        1. Exact ``Rollout/{app_name}`` (canary / Argo Rollouts apps)
        2. Exact ``Deployment/{app_name}``
        3. Preferred ``{app}-api|web|worker|…`` Deployments that exist
        4. Other ``{app}-*`` Deployments excluding known infra suffixes
        """
        app = (app_name or "").strip().lower()
        if not app:
            return []

        if app in self.rollouts:
            return [
                WorkloadRef(
                    kind="Rollout",
                    name=app,
                    api_version="argoproj.io/v1alpha1",
                )
            ]
        if app in self.deployments:
            return [
                WorkloadRef(
                    kind="Deployment",
                    name=app,
                    api_version="apps/v1",
                )
            ]

        preferred: list[WorkloadRef] = []
        for suffix in _PREFERRED_DEPLOY_SUFFIXES:
            name = f"{app}{suffix}"
            if name in self.deployments:
                preferred.append(
                    WorkloadRef(kind="Deployment", name=name, api_version="apps/v1")
                )
        if preferred:
            return preferred

        others: list[WorkloadRef] = []
        prefix = f"{app}-"
        for name in self.deployments:
            if not name.startswith(prefix):
                continue
            if any(name.endswith(sfx) or name == f"{app}{sfx}" for sfx in _INFRA_DEPLOY_SUFFIXES):
                continue
            # Also skip when the remainder is an infra token (pinky-temporal).
            remainder = name[len(prefix):]
            if any(
                remainder == sfx.lstrip("-") or remainder.startswith(sfx.lstrip("-") + "-")
                for sfx in _INFRA_DEPLOY_SUFFIXES
            ):
                continue
            others.append(
                WorkloadRef(kind="Deployment", name=name, api_version="apps/v1")
            )
        return others

    def prompt_block(self, app_name: str) -> str:
        """LLM / skill guidance listing only real scale targets."""
        lines = [
            f"LIVE WORKLOADS in namespace {self.namespace!r} "
            "(HPA scaleTargetRef MUST use one of these — never invent):",
            f"  Deployments: {', '.join(self.deployments) or '(none)'}",
            f"  Rollouts: {', '.join(self.rollouts) or '(none)'}",
        ]
        preferred = self.preferred_scale_targets(app_name)
        if preferred:
            lines.append("Preferred scaleTargetRef(s) for this app:")
            for ref in preferred:
                lines.append(
                    f"  - apiVersion: {ref.api_version}, kind: {ref.kind}, "
                    f"name: {ref.name}"
                )
        else:
            lines.append(
                "No preferred scale target found — emit NOTHING (empty) rather "
                "than guessing Deployment/" + (app_name or "app")
            )
        return "\n".join(lines)


def discover_namespace_label_sets(namespace: str) -> list[dict[str, str]] | None:
    """Return live Service selectors + Deployment pod labels, or ``None`` on failure.

    Used by clear-evidence ``selector_target`` (PDB / ServiceMonitor) — same
    fail-closed posture as HPA live workload discovery.
    """
    ns = (namespace or "").strip()
    if not ns:
        return None
    label_sets: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    def _add(labels: dict | None) -> None:
        if not labels:
            return
        cleaned = {str(k): str(v) for k, v in labels.items() if k and v is not None}
        if not cleaned:
            return
        key = tuple(sorted(cleaned.items()))
        if key in seen:
            return
        seen.add(key)
        label_sets.append(cleaned)

    try:
        from agentit import kube

        try:
            svcs = kube.core_v1().list_namespaced_service(ns, _request_timeout=10)
            for svc in svcs.items:
                spec = getattr(svc, "spec", None)
                _add(getattr(spec, "selector", None) or {})
                meta = getattr(svc, "metadata", None)
                _add(getattr(meta, "labels", None) or {})
        except Exception as exc:
            logger.warning("Failed to list Services in %s: %s", ns, exc)
            return None

        try:
            deps = kube.apps_v1().list_namespaced_deployment(ns, _request_timeout=10)
            for dep in deps.items:
                tmpl = (
                    getattr(getattr(getattr(dep, "spec", None), "template", None), "metadata", None)
                )
                _add(getattr(tmpl, "labels", None) or {})
                meta = getattr(dep, "metadata", None)
                _add(getattr(meta, "labels", None) or {})
        except Exception as exc:
            logger.debug("Deployments unavailable for label sets in %s: %s", ns, exc)

        return label_sets
    except Exception as exc:
        logger.warning("Label-set discovery failed for %s: %s", ns, exc)
        return None


def discover_namespace_workloads(namespace: str) -> NamespaceWorkloads:
    """List Deployments and Rollouts in ``namespace``.

    On API failure, returns ``discovery_ok=False`` with empty lists so callers
    fail closed for HPA content (same posture as self-managed chart gate).
    """
    ns = (namespace or "").strip()
    if not ns:
        return NamespaceWorkloads(
            namespace="", deployments=(), rollouts=(), discovery_ok=False,
        )

    deployments: list[str] = []
    rollouts: list[str] = []
    try:
        from agentit import kube

        try:
            items = kube.apps_v1().list_namespaced_deployment(ns, _request_timeout=10)
            deployments = sorted(
                p.metadata.name for p in items.items if p.metadata and p.metadata.name
            )
        except Exception as exc:
            logger.warning("Failed to list Deployments in %s: %s", ns, exc)
            return NamespaceWorkloads(
                namespace=ns, deployments=(), rollouts=(), discovery_ok=False,
            )

        try:
            raw = kube.list_custom_resources(
                "argoproj.io", "v1alpha1", "rollouts", namespace=ns, timeout=10,
            )
            rollouts = sorted(
                (r.get("metadata") or {}).get("name", "")
                for r in raw
                if (r.get("metadata") or {}).get("name")
            )
        except Exception as exc:
            # Rollout CRD may be absent — Deployments alone are enough.
            logger.debug("Rollouts unavailable in %s: %s", ns, exc)
            rollouts = []

        return NamespaceWorkloads(
            namespace=ns,
            deployments=tuple(deployments),
            rollouts=tuple(rollouts),
            discovery_ok=True,
        )
    except Exception as exc:
        logger.warning("Workload discovery failed for %s: %s", ns, exc)
        return NamespaceWorkloads(
            namespace=ns, deployments=(), rollouts=(), discovery_ok=False,
        )


def _parse_docs(content: str) -> list[dict]:
    try:
        return [d for d in yaml.safe_load_all(content or "") if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def fleet_hpa_correctness_reason(
    content: str,
    workloads: NamespaceWorkloads | None,
    *,
    app_name: str = "",
) -> str | None:
    """Why a fleet HPA must not open a PR, or ``None`` if OK / non-HPA.

    Fail closed when discovery failed or the scale target is missing.
    """
    docs = _parse_docs(content)
    hpas = [d for d in docs if (d.get("kind") or "") == "HorizontalPodAutoscaler"]
    if not hpas:
        return None

    if workloads is None or not workloads.discovery_ok:
        return (
            "HPA scaleTargetRef cannot be verified — namespace workload "
            "discovery failed; refusing fleet HPA rather than inventing a target"
        )

    if not workloads.deployments and not workloads.rollouts:
        return (
            f"no Deployments or Rollouts in namespace {workloads.namespace!r} "
            "— refusing HPA with invented scaleTargetRef"
        )

    for doc in hpas:
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        ref = spec.get("scaleTargetRef") if isinstance(spec.get("scaleTargetRef"), dict) else {}
        kind = str(ref.get("kind") or "").strip()
        name = str(ref.get("name") or "").strip()
        api = str(ref.get("apiVersion") or "").strip()

        if not kind or not name:
            return "HPA scaleTargetRef missing kind or name — refusing"
        if kind not in ("Deployment", "Rollout"):
            return (
                f"HPA scaleTargetRef.kind must be Deployment or Rollout; "
                f"got {kind!r}"
            )
        if kind == "Rollout" and api and "argoproj.io" not in api:
            return (
                "HPA scaleTargetRef.apiVersion must be argoproj.io/* for "
                f"Rollout targets; got {api!r}"
            )
        if kind == "Deployment" and api and not api.startswith("apps/"):
            return (
                "HPA scaleTargetRef.apiVersion must be apps/* for Deployment "
                f"targets; got {api!r}"
            )
        if not workloads.has(kind, name):
            available = []
            if workloads.rollouts:
                available.append("Rollouts: " + ", ".join(workloads.rollouts))
            if workloads.deployments:
                available.append("Deployments: " + ", ".join(workloads.deployments))
            hint = "; ".join(available) or "none"
            preferred = workloads.preferred_scale_targets(app_name)
            prefer_txt = ""
            if preferred:
                prefer_txt = (
                    " Prefer: "
                    + ", ".join(f"{p.kind}/{p.name}" for p in preferred)
                    + "."
                )
            return (
                f"HPA scaleTargetRef {kind}/{name} not found in namespace "
                f"{workloads.namespace!r} (live: {hint}).{prefer_txt} "
                "Refusing invented target (fail closed)"
            )

    return None


def generate_fleet_hpa_yaml(
    app_name: str,
    workloads: NamespaceWorkloads,
    *,
    min_replicas: int = 2,
    max_replicas: int = 10,
    cpu_utilization: int = 80,
) -> str | None:
    """Deterministic HPA YAML for preferred live targets, or ``None`` if none."""
    targets = workloads.preferred_scale_targets(app_name)
    if not targets:
        return None

    docs: list[str] = []
    multi = len(targets) > 1
    for ref in targets:
        hpa_name = f"{ref.name}-hpa" if multi or ref.name != app_name else f"{app_name}-hpa"
        # Keep historical pinky-hpa / {app}-hpa name when single exact target.
        if not multi and ref.name == app_name:
            hpa_name = f"{app_name}-hpa"
        docs.append(
            "\n".join(
                [
                    "apiVersion: autoscaling/v2",
                    "kind: HorizontalPodAutoscaler",
                    "metadata:",
                    f"  name: {hpa_name}",
                    "  labels:",
                    f"    app.kubernetes.io/name: {app_name}",
                    "spec:",
                    "  scaleTargetRef:",
                    f"    apiVersion: {ref.api_version}",
                    f"    kind: {ref.kind}",
                    f"    name: {ref.name}",
                    f"  minReplicas: {min_replicas}",
                    f"  maxReplicas: {max_replicas}",
                    "  metrics:",
                    "    - type: Resource",
                    "      resource:",
                    "        name: cpu",
                    "        target:",
                    "          type: Utilization",
                    f"          averageUtilization: {cpu_utilization}",
                ]
            )
        )
    return "\n---\n".join(docs)


def filter_fleet_hpa_files(
    files: list[dict],
    workloads: NamespaceWorkloads | None,
    *,
    app_name: str = "",
) -> tuple[list[dict], list[str]]:
    """Drop fleet HPA files with bad/unverifiable scaleTargetRef.

    Non-HPA files pass through unchanged.
    """
    kept: list[dict] = []
    reasons: list[str] = []
    for f in files:
        content = f.get("content") or ""
        why = fleet_hpa_correctness_reason(content, workloads, app_name=app_name)
        if why is None:
            kept.append(f)
        else:
            path = f.get("target_path") or f.get("path") or "?"
            reasons.append(f"{path}: {why}")
            logger.info("Fleet HPA filter dropped %s: %s", path, why)
    return kept, reasons


_FLEET_HPA_GENERATION_CONSTRAINTS = (
    "FLEET HORIZONTALPODAUTOSCALER (fail closed if unsure — return empty):\n"
    "- scaleTargetRef.name + kind MUST match a LIVE Deployment or Rollout "
    "listed in the Live Workloads section below.\n"
    "- Prefer an exact Rollout named the app when present "
    "(apiVersion: argoproj.io/v1alpha1, kind: Rollout).\n"
    "- Never invent Deployment/{app_name} when that Deployment does not exist "
    "(common multi-service apps use {app}-api / {app}-web / {app}-worker).\n"
    "- Prefer empty output over an HPA that would not attach "
    "(TARGETS=<unknown> / FailedGetScale).\n"
)


def fleet_hpa_prompt_constraints(app_name: str, workloads: NamespaceWorkloads | None) -> str:
    """Extra LLM user-prompt text for fleet HPA generation."""
    parts = [_FLEET_HPA_GENERATION_CONSTRAINTS]
    if workloads is not None and workloads.discovery_ok:
        parts.append(workloads.prompt_block(app_name))
    else:
        parts.append(
            "Live workload discovery unavailable — emit NOTHING rather than "
            "guessing scaleTargetRef names."
        )
    return "\n".join(parts)
