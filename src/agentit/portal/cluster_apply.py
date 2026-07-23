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
    r"\bForbidden\b|\(403\)|\b403\b.*\bForbidden\b|\bis forbidden\b",
    re.IGNORECASE,
)
_SOFT_MISSING_CRD_RE = re.compile(
    r"no matches for kind|"
    r"not found on cluster|"
    r"could not find the requested resource|"
    r"the server could not find the requested resource|"
    r"unable to recognize|"
    r"\bNot Found\b|\(404\)|\b404\b",
    re.IGNORECASE,
)

# Kinds we always treat as present when discovery is empty/unavailable —
# never capability-filter the whole pack to zero on a probe failure.
_CORE_KINDS_ALWAYS = frozenset({
    "configmap", "configmaps", "secret", "secrets", "service", "services",
    "serviceaccount", "serviceaccounts", "pod", "pods", "namespace", "namespaces",
    "deployment", "deployments", "replicaset", "replicasets",
    "statefulset", "statefulsets", "daemonset", "daemonsets",
    "job", "jobs", "cronjob", "cronjobs",
    "role", "roles", "rolebinding", "rolebindings",
    "clusterrole", "clusterroles", "clusterrolebinding", "clusterrolebindings",
    "networkpolicy", "networkpolicies",
    "horizontalpodautoscaler", "horizontalpodautoscalers",
    "poddisruptionbudget", "poddisruptionbudgets",
    "resourcequota", "resourcequotas", "limitrange", "limitranges",
    "persistentvolumeclaim", "persistentvolumeclaims",
    "ingress", "ingresses",
})


def classify_dry_run_error(message: str | None) -> str:
    """Classify an SSA dry-run failure as ``\"hard\"`` or ``\"soft\"``.

    Soft → skip/warn (Forbidden / missing CRD / Not Found); do **not**
    count as converge failure. Hard → schema/admission/unreachable →
    ``needs_attention`` for cluster packs (source-layer PRs still proceed).
    """
    text = (message or "").strip()
    if not text:
        return "hard"
    if _SOFT_FORBIDDEN_RE.search(text):
        return "soft"
    if _SOFT_MISSING_CRD_RE.search(text):
        return "soft"
    return "hard"


def probe_available_kinds() -> set[str]:
    """Return lowercased GVK kind names available on the live cluster.

    Empty set means probe failed / offline — callers must not treat that
    as "nothing is supported" (see ``filter_files_for_cluster_capabilities``).
    """
    try:
        kinds = kube.get_api_resources()
    except Exception as exc:
        logger.info("Cluster capability probe failed (non-fatal): %s", exc)
        return set()
    return {k.lower() for k in kinds if k}


def _kind_supported(kind: str, available_kinds: set[str]) -> bool:
    """Whether ``kind`` is present on the cluster (or a known core kind)."""
    if not kind:
        return False
    kind_l = kind.lower()
    plural = kind_l if kind_l.endswith("s") else kind_l + "s"
    if kind_l in _CORE_KINDS_ALWAYS or plural in _CORE_KINDS_ALWAYS:
        # Core kinds: if probe succeeded and explicitly lacks them, still
        # allow (discovery glitches must not drop Deployments/ConfigMaps).
        return True
    if not available_kinds:
        # Probe failed — keep optional CRs for SSA soft-skip path rather
        # than silently dropping everything custom.
        return True
    return kind_l in available_kinds or plural in available_kinds


def filter_files_for_cluster_capabilities(
    files: list[dict],
    available_kinds: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Drop cluster YAML whose GVK is absent; keep source / non-YAML.

    Returns ``(kept_files, skip_reasons)``. Prefer not emitting Kyverno /
    Tekton packs when those APIs are missing — capability-gated GitOps.
    Source-layer files (Dockerfile, ``.py``, …) are never filtered here.
    """
    kinds = available_kinds if available_kinds is not None else probe_available_kinds()
    kept: list[dict] = []
    skips: list[str] = []

    for entry in files:
        fpath = entry.get("path") or ""
        suffix = Path(fpath).suffix.lower()
        if suffix not in (".yaml", ".yml"):
            kept.append(entry)
            continue
        docs = _parse_manifest(entry.get("content") or "")
        if not docs:
            kept.append(entry)
            continue
        unsupported = [
            f"{doc.get('kind')} ({doc.get('apiVersion')})"
            for doc in docs
            if doc.get("kind") and doc.get("apiVersion")
            and not _kind_supported(str(doc.get("kind")), kinds)
        ]
        if unsupported and len(unsupported) == len([
            d for d in docs if d.get("kind") and d.get("apiVersion")
        ]):
            reason = (
                f"{fpath}: skipped — API(s) not on cluster: "
                + ", ".join(unsupported)
            )
            skips.append(reason)
            logger.info("Capability filter: %s", reason)
            continue
        if unsupported:
            # Mixed multi-doc file: keep but note partial skip; SSA soft path
            # will skip remaining unsupported docs per-document.
            skips.append(
                f"{fpath}: partial — unsupported kinds will be SSA-skipped: "
                + ", ".join(unsupported)
            )
        kept.append(entry)

    return kept, skips

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
    ``dry_run_manifests_against_cluster()`` can SSA-validate them. When
    ``available_kinds`` is non-empty, missing CRDs are pre-skipped (do not
    count as converge failure). When ``for_dry_run=False`` (legacy classify
    used by skill-parity tests / operator-install helpers), operator-
    namespace and cluster-scoped docs are still skipped: AgentIT never
    direct-applies those.
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

    # Capability gate: skip absent GVKs on both dry-run and apply paths so
    # Tekton/Kyverno packs never burn SSA attempts as converge failures.
    if available_kinds and not _kind_supported(str(kind), available_kinds):
        return "skip_crd_missing", f"{kind} ({api_version}) CRD not installed", doc

    if not for_dry_run:
        if kind in _CLUSTER_SCOPED_KINDS:
            return "skip_cluster_scope", f"{kind} is cluster-scoped (needs cluster-admin)", doc
        if manifest_ns in _OPERATOR_NAMESPACES:
            return "skip_operator_ns", f"targets operator namespace {manifest_ns}", doc

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
    *,
    available_kinds: set[str] | None = None,
) -> dict:
    """Validate manifests via Kubernetes server-side-apply ``dryRun=All``.

    Calls ``kube.apply_yaml(..., dry_run=True)`` for every concrete YAML
    document that would be delivered -- never persists anything on the
    cluster. This is the portal/auto_delivery Dry Run path (GitOps remains
    the sole real apply via PR merge + Argo).

    Failures are classified via ``classify_dry_run_error``:
    - **hard** (``errors``): schema/Bad Request, admission, unreachable —
      block cluster PR / ``needs_attention`` (source-layer still proceeds)
    - **soft** (``warnings`` + ``skipped``): Forbidden (SA RBAC), missing
      optional CRD / GVK / Not Found — skip that file; do **not** count as
      converge failure. Field-manager conflict is also soft on dry-run.

    Field-manager conflicts are soft on dry-run: AgentIT is not seizing
    ownership here (GitOps/Argo applies after merge). Re-onboarding an app
    with prior ``kubectl`` client-side-apply ConfigMaps must not hard-block
    PR open. Structured ``conflicts`` is still populated for UI/PR notes.
    Non-YAML / narrative files are listed under ``repo_files`` and skipped.

    ``skipped_paths`` lists file paths soft-skipped (missing API / Forbidden)
    so callers can drop them from delivery packs.
    """
    kinds = available_kinds if available_kinds is not None else probe_available_kinds()
    applied: list[str] = []
    skipped: list[str] = []
    skipped_paths: list[str] = []
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
                doc, namespace, available_kinds=kinds, for_dry_run=True,
            )
            if action == "apply":
                apply_docs.append(fixed)
            else:
                skip_reasons.append(reason)
                if action == "skip_crd_missing":
                    kind = doc.get("kind", "")
                    if kind in _CRD_TO_OPERATOR:
                        missing_operators[kind] = _CRD_TO_OPERATOR[kind]

        if not apply_docs:
            tagged = f"{fpath} ({'; '.join(skip_reasons) or 'nothing to validate'})"
            skipped.append(tagged)
            skipped_paths.append(fpath)
            warnings.append(
                f"{fpath}: skipped — missing API / unsupported on cluster "
                f"({'; '.join(skip_reasons)})"
            )
            continue

        content = yaml.dump_all(apply_docs, default_flow_style=False)
        try:
            result = kube.apply_yaml(content, namespace, dry_run=True)
        except Exception as exc:
            # Fail closed: unreachable cluster / AGENTIT_OFFLINE / discovery
            # blow-ups must never look like a clean dry-run.
            err = str(exc)
            if classify_dry_run_error(err) == "soft":
                skipped.append(f"{fpath} ({err})")
                skipped_paths.append(fpath)
                warnings.append(f"{fpath}: {err}")
                logger.warning("Dry-run soft skip for %s: %s", fpath, err)
            else:
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
            # Soft: dry-run does not take ownership; Argo sync after merge does.
            tagged = f"{fpath}: {result['error']}"
            warnings.append(tagged)
            logger.warning(
                "Dry-run field-manager conflict for %s (non-blocking): %s",
                fpath, result["error"],
            )
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
                            or "not found" in msg.lower()
                            or "404" in msg.lower()
                        ):
                            missing_operators[kind] = _CRD_TO_OPERATOR[kind]
                else:
                    file_hard.append(tagged)
            if file_hard:
                errors.extend(file_hard)
                logger.error("Dry-run hard failure for %s: %s", fpath, "; ".join(file_hard))
            if file_soft:
                # Soft = skip this file (Forbidden / missing CRD), not converge-fail.
                skipped.append(f"{fpath} ({'; '.join(file_soft)})")
                skipped_paths.append(fpath)
                warnings.extend(file_soft)
                logger.warning(
                    "Dry-run soft skip for %s (non-blocking): %s",
                    fpath, "; ".join(file_soft),
                )
            if not file_hard and not file_soft:
                errors.append(f"{fpath}: unknown dry-run failure")

    return {
        "applied": applied,
        "skipped": skipped,
        "skipped_paths": skipped_paths,
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
