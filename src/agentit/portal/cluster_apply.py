from __future__ import annotations

import logging
from pathlib import Path

import yaml

from agentit import kube

logger = logging.getLogger(__name__)

_SKIP_EXTENSIONS = frozenset({".sh", ".md", ".json", ".txt", ".toml", ".cfg", ".ini"})

_NON_MANIFEST_PURPOSE: dict[str, str] = {
    ".md": "Documentation — review in your repo after merging the PR",
    ".json": "Configuration — commit to your repo (e.g. Renovate config, Grafana dashboard)",
    ".sh": "Script — should be a Tekton Task instead; report this as a bug",
    ".toml": "Configuration — commit to your repo",
    ".cfg": "Configuration — commit to your repo",
    ".ini": "Configuration — commit to your repo",
    ".txt": "Configuration — commit to your repo",
}

_OPERATOR_NAMESPACES = frozenset({
    "openshift-gitops", "openshift-operators", "openshift-pipelines",
    "openshift-monitoring", "openshift-logging",
})

_CRD_TO_OPERATOR: dict[str, dict] = {
    # ── Red Hat Operators (installable via UI) ─────────────────────
    "VerticalPodAutoscaler": {
        "name": "VPA (Vertical Pod Autoscaler)",
        "package": "vertical-pod-autoscaler",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Automatically right-sizes CPU and memory requests based on actual usage — reduces waste and prevents OOM kills.",
        # Only supports the OwnNamespace install mode (verified against this
        # cluster's packagemanifest) -- needs a dedicated namespace, can't join
        # the shared openshift-operators AllNamespaces OperatorGroup.
        "own_namespace": True,
    },
    "Pipeline": {
        "name": "OpenShift Pipelines",
        "package": "openshift-pipelines-operator-rh",
        "channel": "latest",
        "source": "redhat-operators",
        "value": "Automated CI/CD with Tekton — build, test, scan, and deploy on every commit.",
    },
    "Task": {
        "name": "OpenShift Pipelines",
        "package": "openshift-pipelines-operator-rh",
        "channel": "latest",
        "source": "redhat-operators",
        "value": "Required for running image scans and SBOM generation in your CI pipeline.",
    },
    "PipelineRun": {
        "name": "OpenShift Pipelines",
        "package": "openshift-pipelines-operator-rh",
        "channel": "latest",
        "source": "redhat-operators",
        "value": "Required for running CI/CD pipelines.",
    },
    "Rollout": {
        "name": "OpenShift GitOps",
        "package": "openshift-gitops-operator",
        "channel": "latest",
        "source": "redhat-operators",
        "value": "Canary deployments with automatic rollback — reduce blast radius of bad deploys.",
    },
    "Application": {
        "name": "OpenShift GitOps",
        "package": "openshift-gitops-operator",
        "channel": "latest",
        "source": "redhat-operators",
        "value": "GitOps delivery — your Git repo is the source of truth for cluster state.",
    },
    "AnalysisTemplate": {
        "name": "OpenShift GitOps",
        "package": "openshift-gitops-operator",
        "channel": "latest",
        "source": "redhat-operators",
        "value": "Automated deployment analysis — verify metrics before promoting a canary.",
    },
    "OpenTelemetryCollector": {
        "name": "Red Hat OpenTelemetry",
        "package": "opentelemetry-product",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Unified traces, metrics, and logs collection — see exactly what your app does in production.",
    },
    "ServiceMeshControlPlane": {
        "name": "Red Hat OpenShift Service Mesh",
        "package": "servicemeshoperator",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "mTLS between services, traffic management, and network-level observability.",
    },
    "NMState": {
        "name": "Kubernetes NMState Operator",
        "package": "kubernetes-nmstate-operator",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Declarative network configuration for nodes.",
    },
    "StorageCluster": {
        "name": "OpenShift Data Foundation",
        "package": "odf-operator",
        "channel": "stable-4.17",
        "source": "redhat-operators",
        "value": "Persistent storage, object storage, and backup infrastructure for stateful apps.",
        "own_namespace": True,  # OwnNamespace-only; see VerticalPodAutoscaler note above.
    },
    "ACSSecuredCluster": {
        "name": "Advanced Cluster Security",
        "package": "rhacs-operator",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Runtime vulnerability scanning, compliance checks, and network segmentation policies.",
    },
    "Central": {
        "name": "Advanced Cluster Security",
        "package": "rhacs-operator",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Central security management — CVE database, policy engine, and compliance dashboard.",
    },
    "KnativeServing": {
        "name": "Red Hat OpenShift Serverless",
        "package": "serverless-operator",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Scale-to-zero and autoscaling for event-driven workloads — pay only for what you use.",
    },
    "KnativeEventing": {
        "name": "Red Hat OpenShift Serverless",
        "package": "serverless-operator",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Event-driven architecture — connect services with CloudEvents.",
    },
    "ClusterLogging": {
        "name": "Red Hat OpenShift Logging",
        "package": "cluster-logging",
        "channel": "stable-6.2",
        "source": "redhat-operators",
        "value": "Centralized log collection and forwarding — aggregate logs from all pods to Loki or Elasticsearch.",
    },
    "Kafka": {
        "name": "AMQ Streams",
        "package": "amq-streams",
        "channel": "stable",
        "source": "redhat-operators",
        "value": "Event streaming backbone — durable, ordered messaging for agent communication and audit trails.",
    },
    "Keycloak": {
        "name": "Red Hat build of Keycloak",
        "package": "rhbk-operator",
        "channel": "stable-v26",
        "source": "redhat-operators",
        "value": "SSO, OIDC, and SAML authentication — secure your app without building auth from scratch.",
        "own_namespace": True,  # OwnNamespace-only; see VerticalPodAutoscaler note above.
    },
    # ── Built-in (no install needed) ──────────────────────────────
    "PrometheusRule": {
        "name": "Cluster Monitoring (built-in)",
        "package": None,
        "note": "PrometheusRule requires the OpenShift monitoring stack (enabled by default).",
    },
    "ServiceMonitor": {
        "name": "Cluster Monitoring (built-in)",
        "package": None,
        "note": "ServiceMonitor requires the OpenShift monitoring stack.",
    },
    # ── Community (manual install only) ───────────────────────────
    "Policy": {
        "name": "Kyverno (community)",
        "package": None,
        "note": "Kyverno is a community operator — install manually if needed.",
    },
    "ChaosEngine": {
        "name": "LitmusChaos (community)",
        "package": None,
        "note": "LitmusChaos is not in the Red Hat catalog.",
    },
}

_CLUSTER_SCOPED_KINDS = frozenset({
    "ClusterRole", "ClusterRoleBinding", "ClusterPolicy",
    "ClusterCleanupPolicy", "CustomResourceDefinition",
    "StorageClass", "PriorityClass", "ClusterIssuer",
})


def _get_available_resources() -> set[str]:
    """Get available API resource kinds on the cluster."""
    return kube.get_api_resources()


def _ensure_namespace(namespace: str, dry_run: bool) -> None:
    if not kube.namespace_exists(namespace) and not dry_run:
        kube.create_namespace(namespace)
        logger.info("Created namespace %s", namespace)


def _parse_manifest(content: str) -> list[dict]:
    """Parse YAML content into a list of K8s-like documents."""
    try:
        docs = [d for d in yaml.safe_load_all(content) if isinstance(d, dict)]
        return docs
    except yaml.YAMLError:
        return []


def _classify_and_fix(
    doc: dict, namespace: str, available_kinds: set[str],
) -> tuple[str, str, dict]:
    """Classify a manifest document and fix namespace if needed.

    Returns (action, reason, fixed_doc) where action is one of:
        apply, skip_non_k8s, skip_cluster_scope, skip_operator_ns,
        skip_crd_missing
    """
    kind = doc.get("kind", "")
    api_version = doc.get("apiVersion", "")

    if not kind or not api_version:
        return "skip_non_k8s", "not a K8s manifest (missing kind/apiVersion)", doc

    if kind in _CLUSTER_SCOPED_KINDS:
        return "skip_cluster_scope", f"{kind} is cluster-scoped (needs cluster-admin)", doc

    meta = doc.get("metadata") or {}
    manifest_ns = meta.get("namespace", "")

    if manifest_ns in _OPERATOR_NAMESPACES:
        return "skip_operator_ns", f"targets operator namespace {manifest_ns}", doc

    if available_kinds:
        kind_lower = kind.lower()
        kind_plural_guess = kind_lower + "s"
        if kind_lower not in available_kinds and kind_plural_guess not in available_kinds:
            return "skip_crd_missing", f"{kind} ({api_version}) CRD not installed", doc

    if manifest_ns and manifest_ns != namespace:
        meta["namespace"] = namespace

    if "generateName" in meta and "name" not in meta:
        meta["name"] = meta.pop("generateName").rstrip("-") + "-applied"

    return "apply", "", doc


def apply_manifests_to_cluster(
    files: list[dict],
    namespace: str = "default",
    dry_run: bool = False,
) -> dict:
    """Apply manifests to the cluster with pre-flight validation."""
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    missing_operators: dict[str, dict] = {}
    repo_files: list[dict] = []

    _ensure_namespace(namespace, dry_run)
    available = _get_available_resources()

    for entry in files:
        fpath = entry["path"]
        suffix = Path(fpath).suffix.lower()

        if suffix in _SKIP_EXTENSIONS or suffix not in (".yaml", ".yml"):
            purpose = _NON_MANIFEST_PURPOSE.get(suffix, "Not a Kubernetes manifest")
            repo_files.append({
                "path": fpath,
                "purpose": purpose,
                "description": entry.get("description", ""),
            })
            continue

        docs = _parse_manifest(entry["content"])
        if not docs:
            skipped.append(f"{fpath} (empty or unparseable)")
            continue

        all_skip = True
        skip_reasons = []
        apply_docs = []

        for doc in docs:
            action, reason, fixed = _classify_and_fix(doc, namespace, available)
            if action == "apply":
                all_skip = False
                apply_docs.append(fixed)
            else:
                skip_reasons.append(reason)
                if action == "skip_crd_missing":
                    kind = doc.get("kind", "")
                    if kind in _CRD_TO_OPERATOR:
                        op = _CRD_TO_OPERATOR[kind]
                        missing_operators[kind] = op

        if all_skip:
            skipped.append(f"{fpath} ({'; '.join(skip_reasons)})")
            continue

        content = yaml.dump_all(apply_docs, default_flow_style=False)

        if dry_run:
            applied.append(fpath)
            logger.info("Dry-run validated %s", fpath)
        else:
            result = kube.apply_yaml(content, namespace)
            if result["applied"]:
                applied.append(fpath)
                logger.info("Applied %s", fpath)
            else:
                errors.append(f"{fpath}: {result['error']}")
                logger.error("Failed %s: %s", fpath, result["error"])

    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "missing_operators": missing_operators,
        "repo_files": repo_files,
    }


# Packages whose CSV only supports the OwnNamespace install mode, so they must
# land in a dedicated namespace with their own OperatorGroup rather than joining
# the shared openshift-operators AllNamespaces OperatorGroup. Sourced from the
# per-CRD "own_namespace" flags above (deduped by package, since several kinds
# can map to the same operator).
_OWN_NAMESPACE_PACKAGES: frozenset[str] = frozenset(
    op["package"] for op in _CRD_TO_OPERATOR.values()
    if op.get("package") and op.get("own_namespace")
)

_RBAC_HELP = (
    "AgentIT's service account lacks the cluster permission this install needs. "
    "Ask a cluster admin to enable `rbac.operatorInstall` in the Helm chart values "
    "(grants the minimal OLM permissions for operator installs), or install "
    "the operator manually via OperatorHub in the console."
)


def install_operator(package: str, channel: str, source: str) -> dict:
    """Install an OLM operator via Subscription CR.

    Creates a dedicated namespace with a scoped OperatorGroup for operators that
    only support the OwnNamespace install mode (e.g. VPA, ODF, Keycloak/RHBK --
    see ``_OWN_NAMESPACE_PACKAGES``). All other operators are installed as a
    Subscription in the existing ``openshift-operators`` namespace, which already
    has an AllNamespaces OperatorGroup -- no new namespace needed.
    """
    if source != "redhat-operators":
        return {"status": "error", "package": package,
                "error": f"Only Red Hat operators are supported (source={source})"}

    own_namespace = package in _OWN_NAMESPACE_PACKAGES

    if own_namespace:
        ns = f"openshift-{package.replace('_', '-')}"
        docs = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": ns},
            },
            {
                "apiVersion": "operators.coreos.com/v1",
                "kind": "OperatorGroup",
                "metadata": {"name": f"{package}-og", "namespace": ns},
                "spec": {"targetNamespaces": [ns]},
            },
            {
                "apiVersion": "operators.coreos.com/v1alpha1",
                "kind": "Subscription",
                "metadata": {"name": package, "namespace": ns},
                "spec": {
                    "channel": channel,
                    "name": package,
                    "source": source,
                    "sourceNamespace": "openshift-marketplace",
                    "installPlanApproval": "Automatic",
                },
            },
        ]
    else:
        ns = "openshift-operators"
        docs = [
            {
                "apiVersion": "operators.coreos.com/v1alpha1",
                "kind": "Subscription",
                "metadata": {"name": package, "namespace": ns},
                "spec": {
                    "channel": channel,
                    "name": package,
                    "source": source,
                    "sourceNamespace": "openshift-marketplace",
                    "installPlanApproval": "Automatic",
                },
            },
        ]

    content = yaml.dump_all(docs, default_flow_style=False)
    result = kube.apply_yaml(content, ns)
    if result["applied"]:
        logger.info("Operator %s install started in %s", package, ns)
        return {"status": "installing", "package": package, "namespace": ns}

    error = result["error"] or "unknown"
    if "forbidden" in error.lower():
        logger.error("Operator %s install forbidden in %s: %s", package, ns, error)
        return {"status": "error", "package": package, "namespace": ns,
                 "error": f"{_RBAC_HELP} (server said: {error})"}

    logger.error("Operator %s install failed: %s", package, error)
    return {"status": "error", "package": package, "namespace": ns, "error": error}
