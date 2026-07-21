from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from agentit import kube

logger = logging.getLogger(__name__)

# Soft: AgentIT SA cannot dry-run every Role/LimitRange; optional operator
# CRDs (Kyverno, Litmus, …) may be absent. Neither means the manifest is
# invalid for GitOps/Argo after merge. Hard: schema/admission/unreachable.
_SOFT_FORBIDDEN_RE = re.compile(
    r"\bForbidden\b|\(403\)|\b403\b.*\bForbidden\b",
    re.IGNORECASE,
)
_SOFT_MISSING_CRD_RE = re.compile(
    r"no matches for kind|"
    r"not found on cluster|"
    r"could not find the requested resource|"
    r"the server could not find the requested resource|"
    r"unable to recognize",
    re.IGNORECASE,
)


def classify_dry_run_error(message: str | None) -> str:
    """Classify an SSA dry-run failure as ``\"hard\"`` or ``\"soft\"``.

    Soft → warn in PR body / apply_results; do not block Scan when hard
    errors are empty. Hard → ``needs_attention`` / block PR open.
    """
    text = (message or "").strip()
    if not text:
        return "hard"
    if _SOFT_FORBIDDEN_RE.search(text):
        return "soft"
    if _SOFT_MISSING_CRD_RE.search(text):
        return "soft"
    return "hard"

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


def _parse_manifest(content: str) -> list[dict]:
    """Parse YAML content into a list of K8s-like documents."""
    try:
        docs = [d for d in yaml.safe_load_all(content) if isinstance(d, dict)]
        return docs
    except yaml.YAMLError:
        return []


def _classify_and_fix(
    doc: dict, namespace: str, available_kinds: set[str],
    *, for_dry_run: bool = False,
) -> tuple[str, str, dict]:
    """Classify a manifest document and fix namespace if needed.

    Returns (action, reason, fixed_doc) where action is one of:
        apply, skip_non_k8s, skip_cluster_scope, skip_operator_ns,
        skip_crd_missing

    ``for_dry_run=True`` prepares documents the way GitOps would eventually
    land them (including operator-namespace and cluster-scoped kinds) so
    ``dry_run_manifests_against_cluster()`` can SSA-validate them. Missing
    CRDs are *not* pre-skipped on that path -- ``kube.apply_yaml(dry_run=True)``
    surfaces a clear apiserver/discovery error instead (fail-closed).
    When ``for_dry_run=False`` (legacy classify used by skill-parity tests /
    operator-install helpers), operator-namespace and cluster-scoped docs
    are still skipped: AgentIT never direct-applies those.
    """
    kind = doc.get("kind", "")
    api_version = doc.get("apiVersion", "")

    if not kind or not api_version:
        return "skip_non_k8s", "not a K8s manifest (missing kind/apiVersion)", doc

    meta = doc.get("metadata") or {}
    # Work on a shallow copy so callers' input docs are never mutated.
    doc = {**doc, "metadata": dict(meta)}
    meta = doc["metadata"]
    manifest_ns = meta.get("namespace", "")

    if not for_dry_run:
        if kind in _CLUSTER_SCOPED_KINDS:
            return "skip_cluster_scope", f"{kind} is cluster-scoped (needs cluster-admin)", doc
        if manifest_ns in _OPERATOR_NAMESPACES:
            return "skip_operator_ns", f"targets operator namespace {manifest_ns}", doc
        if available_kinds:
            kind_lower = kind.lower()
            kind_plural_guess = kind_lower + "s"
            if kind_lower not in available_kinds and kind_plural_guess not in available_kinds:
                return "skip_crd_missing", f"{kind} ({api_version}) CRD not installed", doc

    if kind not in _CLUSTER_SCOPED_KINDS:
        if manifest_ns in _OPERATOR_NAMESPACES:
            pass  # keep declared operator namespace (GitOps / dry-run)
        elif manifest_ns and manifest_ns != namespace:
            meta["namespace"] = namespace
        elif for_dry_run and not manifest_ns:
            # SSA dry-run needs an explicit namespace; real apply paths pass
            # the target namespace into kube.apply_yaml() instead.
            meta["namespace"] = namespace

    if "generateName" in meta and "name" not in meta:
        meta["name"] = meta.pop("generateName").rstrip("-") + "-applied"

    return "apply", "", doc


def dry_run_manifests_against_cluster(
    files: list[dict],
    namespace: str = "default",
) -> dict:
    """Validate manifests via Kubernetes server-side-apply ``dryRun=All``.

    Calls ``kube.apply_yaml(..., dry_run=True)`` for every concrete YAML
    document that would be delivered -- never persists anything on the
    cluster. This is the portal/auto_delivery Dry Run path (GitOps remains
    the sole real apply via PR merge + Argo).

    Failures are classified via ``classify_dry_run_error``:
    - **hard** (``errors``): schema/Bad Request, admission, unreachable —
      block PR / ``needs_attention``
    - **soft** (``warnings``): Forbidden (SA lacks dry-run RBAC), missing
      optional CRD / GVK — warn in PR body; do not block when hard is empty

    Field-manager conflicts stay hard (ownership fight needs a human).
    Non-YAML / narrative files are listed under ``repo_files`` and skipped.
    """
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    conflicts: list[dict] = []
    missing_operators: dict[str, dict] = {}
    repo_files: list[dict] = []

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

        docs = _parse_manifest(entry.get("content") or "")
        if not docs:
            skipped.append(f"{fpath} (empty or unparseable)")
            continue

        apply_docs: list[dict] = []
        skip_reasons: list[str] = []
        for doc in docs:
            action, reason, fixed = _classify_and_fix(
                doc, namespace, available_kinds=set(), for_dry_run=True,
            )
            if action == "apply":
                apply_docs.append(fixed)
            else:
                skip_reasons.append(reason)

        if not apply_docs:
            skipped.append(f"{fpath} ({'; '.join(skip_reasons) or 'nothing to validate'})")
            continue

        content = yaml.dump_all(apply_docs, default_flow_style=False)
        try:
            result = kube.apply_yaml(content, namespace, dry_run=True)
        except Exception as exc:
            # Fail closed: unreachable cluster / AGENTIT_OFFLINE / discovery
            # blow-ups must never look like a clean dry-run.
            err = str(exc)
            errors.append(f"{fpath}: {err}")
            logger.error("Dry-run failed for %s: %s", fpath, err)
            continue
        if result["applied"]:
            applied.append(fpath)
            logger.info("Dry-run (SSA dryRun=All): %s validated cleanly", fpath)
        elif result.get("conflict"):
            conflicts.append({
                "path": fpath, "error": result["error"],
                "details": result.get("conflict_details", []),
            })
            logger.warning("Dry-run field-manager conflict for %s: %s", fpath, result["error"])
        else:
            # Prefer per-document ``errors`` so a soft Forbidden on doc 1
            # cannot hide a hard Bad Request on doc 2.
            per_doc = list(result.get("errors") or [])
            if not per_doc and result.get("error"):
                per_doc = [str(result["error"])]
            file_hard: list[str] = []
            file_soft: list[str] = []
            for msg in per_doc:
                tagged = f"{fpath}: {msg}"
                if classify_dry_run_error(msg) == "soft":
                    file_soft.append(tagged)
                    for doc in apply_docs:
                        kind = doc.get("kind", "")
                        if kind in _CRD_TO_OPERATOR and (
                            "not found on cluster" in msg.lower()
                            or "no matches for kind" in msg.lower()
                            or "could not find the requested resource" in msg.lower()
                            or "unable to recognize" in msg.lower()
                        ):
                            missing_operators[kind] = _CRD_TO_OPERATOR[kind]
                else:
                    file_hard.append(tagged)
            if file_hard:
                errors.extend(file_hard)
                logger.error("Dry-run hard failure for %s: %s", fpath, "; ".join(file_hard))
            if file_soft:
                warnings.extend(file_soft)
                logger.warning(
                    "Dry-run soft warning for %s (non-blocking): %s",
                    fpath, "; ".join(file_soft),
                )
            if not file_hard and not file_soft:
                errors.append(f"{fpath}: unknown dry-run failure")

    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        "conflicts": conflicts,
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
