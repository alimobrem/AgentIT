"""Platform context — discovers what the cluster supports for context-aware generation."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PlatformContext:
    """Snapshot of what the current cluster supports."""
    k8s_version: str = "unknown"
    ocp_version: str | None = None
    available_api_groups: dict[str, list[str]] = field(default_factory=dict)
    available_kinds: set[str] = field(default_factory=set)
    installed_crds: list[str] = field(default_factory=list)
    installed_operators: list[str] = field(default_factory=list)
    deprecated_apis: list[dict] = field(default_factory=list)
    namespace: str = "default"
    _lower_kinds: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self._lower_kinds = {k.lower() for k in self.available_kinds}

    def has_api(self, kind: str) -> bool:
        """Check if a K8s resource kind is available on this cluster."""
        return kind.lower() in self._lower_kinds

    def api_version_for(self, kind: str) -> str | None:
        """Get the preferred API version for a resource kind."""
        for group, versions in self.available_api_groups.items():
            if kind.lower() in group.lower():
                return versions[0] if versions else None
        return None

    def has_operator(self, name: str) -> bool:
        """Check if an operator is installed."""
        return any(name.lower() in op.lower() for op in self.installed_operators)

    def has_crd(self, crd_name: str) -> bool:
        """Check if a CRD is installed."""
        return any(crd_name.lower() in c.lower() for c in self.installed_crds)

    def summary(self) -> str:
        parts = [f"K8s {self.k8s_version}"]
        if self.ocp_version:
            parts.append(f"OpenShift {self.ocp_version}")
        parts.append(f"{len(self.available_kinds)} API kinds")
        parts.append(f"{len(self.installed_crds)} CRDs")
        parts.append(f"{len(self.installed_operators)} operators")
        if self.deprecated_apis:
            parts.append(f"{len(self.deprecated_apis)} deprecated APIs")
        return ", ".join(parts)

    def to_prompt_context(self) -> str:
        """Format as context for LLM prompts."""
        lines = [
            f"Cluster: Kubernetes {self.k8s_version}",
        ]
        if self.ocp_version:
            lines.append(f"Platform: OpenShift {self.ocp_version}")
        if self.installed_operators:
            lines.append(f"Installed operators: {', '.join(self.installed_operators[:20])}")
        if self.deprecated_apis:
            lines.append("Deprecated APIs (DO NOT USE):")
            for d in self.deprecated_apis:
                lines.append(f"  - {d.get('api', '')} (removed in {d.get('removed_in', '?')})")
        lines.append(f"Available resource kinds: {', '.join(sorted(self.available_kinds)[:50])}")
        return "\n".join(lines)


# Well-known K8s API deprecations by version
_KNOWN_DEPRECATIONS = [
    {"api": "extensions/v1beta1 Ingress", "deprecated_in": "1.14", "removed_in": "1.22", "replacement": "networking.k8s.io/v1 Ingress"},
    {"api": "policy/v1beta1 PodSecurityPolicy", "deprecated_in": "1.21", "removed_in": "1.25", "replacement": "Pod Security Standards"},
    {"api": "autoscaling/v2beta1 HorizontalPodAutoscaler", "deprecated_in": "1.23", "removed_in": "1.26", "replacement": "autoscaling/v2"},
    {"api": "batch/v1beta1 CronJob", "deprecated_in": "1.21", "removed_in": "1.25", "replacement": "batch/v1"},
    {"api": "flowcontrol.apiserver.k8s.io/v1beta1", "deprecated_in": "1.26", "removed_in": "1.29", "replacement": "flowcontrol.apiserver.k8s.io/v1"},
    {"api": "tekton.dev/v1beta1 Pipeline", "deprecated_in": "0.44", "removed_in": "1.0", "replacement": "tekton.dev/v1"},
    {"api": "tekton.dev/v1beta1 Task", "deprecated_in": "0.44", "removed_in": "1.0", "replacement": "tekton.dev/v1"},
]


def discover_platform(namespace: str = "") -> PlatformContext:
    """Query the cluster and build a PlatformContext. Gracefully degrades when off-cluster."""
    ns = namespace or os.environ.get("AGENTIT_NAMESPACE", "default")
    ctx = PlatformContext(namespace=ns)

    try:
        from agentit import kube

        # K8s version
        try:
            client = kube.get_client()
            version_info = client.VersionApi().get_code()
            ctx.k8s_version = f"{version_info.major}.{version_info.minor}".rstrip("+")
        except Exception as exc:
            logger.debug("Failed to get K8s version: %s", exc)

        # OpenShift version (check for openshift API group)
        try:
            api_client = kube.get_client().ApiClient()
            resp = api_client.call_api(
                "/apis/config.openshift.io/v1/clusterversions",
                "GET", _return_http_data_only=True, _preload_content=False,
            )
            import json
            data = json.loads(resp.read())
            items = data.get("items", [])
            if items:
                ctx.ocp_version = items[0].get("status", {}).get("desired", {}).get("version", "")
        except Exception:
            pass  # Not OpenShift

        # Available API resources
        try:
            from kubernetes.client import ApisApi
            groups = ApisApi(kube.get_client().ApiClient()).get_api_versions()
            for g in groups.groups:
                name = g.name
                versions = [v.version for v in g.versions]
                ctx.available_api_groups[name] = versions
        except Exception as exc:
            logger.debug("Failed to list API groups: %s", exc)

        # Available kinds
        try:
            ctx.available_kinds = kube.get_api_resources()
        except Exception as exc:
            logger.debug("Failed to list API resources: %s", exc)

        # Installed CRDs
        try:
            from kubernetes.client import ApiextensionsV1Api
            crds = ApiextensionsV1Api(kube.get_client().ApiClient()).list_custom_resource_definition()
            ctx.installed_crds = [c.metadata.name for c in crds.items]
        except Exception as exc:
            logger.debug("Failed to list CRDs: %s", exc)

        # Installed operators (check for CSVs in the namespace)
        try:
            csvs = kube.list_custom_resources(
                "operators.coreos.com", "v1alpha1", "clusterserviceversions", namespace=ns,
            )
            ctx.installed_operators = [
                csv.get("metadata", {}).get("name", "")
                for csv in csvs
                if csv.get("status", {}).get("phase") == "Succeeded"
            ]
        except Exception as exc:
            logger.debug("Failed to list operators: %s", exc)

        # Check for deprecated APIs in use
        ctx.deprecated_apis = _check_deprecations(ctx.k8s_version)

    except ImportError:
        logger.info("kubernetes client not available — using empty PlatformContext")
    except Exception as exc:
        logger.warning("Platform discovery failed: %s", exc)

    return ctx


def _check_deprecations(k8s_version: str) -> list[dict]:
    """Check which known deprecations apply to this K8s version."""
    deprecated = []
    try:
        major_minor = tuple(int(x) for x in k8s_version.split(".")[:2])
    except (ValueError, IndexError):
        return deprecated

    for dep in _KNOWN_DEPRECATIONS:
        try:
            dep_version = tuple(int(x) for x in dep["deprecated_in"].split(".")[:2])
            if major_minor >= dep_version:
                deprecated.append(dep)
        except (ValueError, IndexError):
            continue
    return deprecated


def offline_context(k8s_version: str = "1.28", ocp_version: str | None = "4.15") -> PlatformContext:
    """Create a PlatformContext without cluster access (for testing/dev)."""
    common_kinds = {
        "pods", "services", "deployments", "replicasets", "statefulsets",
        "daemonsets", "jobs", "cronjobs", "configmaps", "secrets",
        "serviceaccounts", "roles", "rolebindings", "clusterroles",
        "clusterrolebindings", "networkpolicies", "ingresses",
        "horizontalpodautoscalers", "poddisruptionbudgets",
        "persistentvolumeclaims", "storageclasses", "namespaces",
    }
    return PlatformContext(
        k8s_version=k8s_version,
        ocp_version=ocp_version,
        available_kinds=common_kinds,
        deprecated_apis=_check_deprecations(k8s_version),
    )
