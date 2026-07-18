from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from agentit import kube
from agentit.audit import audit_log
from agentit.skill_engine import record_skill_outcomes

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
    *, allow_operator_namespaces: bool = False,
) -> tuple[str, str, dict]:
    """Classify a manifest document and fix namespace if needed.

    Returns (action, reason, fixed_doc) where action is one of:
        apply, skip_non_k8s, skip_cluster_scope, skip_operator_ns,
        skip_crd_missing

    ``allow_operator_namespaces=True`` used to be set by the unified
    delivery router's ``cluster-admin-review`` gate approval path
    (``portal/delivery.py``/``routes/gates.py``) -- a human holding elevated
    RBAC explicitly approving this exact apply, so the manifest's own
    declared operator namespace (e.g. ``openshift-pipelines``) was preserved
    and applied as-is instead of being skipped or silently rewritten to the
    app's own namespace. That gate type (and every direct-cluster-apply code
    path in this app) was retired 2026-07-18 -- CI/CD manifests destined for
    a shared operator namespace now deliver via a GitOps PR instead, same as
    every other category (see the README). No live caller passes
    ``allow_operator_namespaces=True`` anymore; kept only as documented,
    still-correct behavior in case a future caller genuinely needs it again.
    """
    kind = doc.get("kind", "")
    api_version = doc.get("apiVersion", "")

    if not kind or not api_version:
        return "skip_non_k8s", "not a K8s manifest (missing kind/apiVersion)", doc

    if kind in _CLUSTER_SCOPED_KINDS:
        return "skip_cluster_scope", f"{kind} is cluster-scoped (needs cluster-admin)", doc

    meta = doc.get("metadata") or {}
    manifest_ns = meta.get("namespace", "")

    if manifest_ns in _OPERATOR_NAMESPACES and not allow_operator_namespaces:
        return "skip_operator_ns", f"targets operator namespace {manifest_ns}", doc

    if available_kinds:
        kind_lower = kind.lower()
        kind_plural_guess = kind_lower + "s"
        if kind_lower not in available_kinds and kind_plural_guess not in available_kinds:
            return "skip_crd_missing", f"{kind} ({api_version}) CRD not installed", doc

    if manifest_ns in _OPERATOR_NAMESPACES and allow_operator_namespaces:
        # Preserve the manifest's own declared operator namespace -- never
        # rewrite it to the app's namespace the way the branch below does
        # for ordinary manifests.
        pass
    elif manifest_ns and manifest_ns != namespace:
        meta["namespace"] = namespace

    if "generateName" in meta and "name" not in meta:
        meta["name"] = meta.pop("generateName").rstrip("-") + "-applied"

    return "apply", "", doc


def apply_manifests_to_cluster(
    files: list[dict],
    namespace: str = "default",
    dry_run: bool = False,
    *, allow_operator_namespaces: bool = False,
) -> dict:
    """Apply manifests to the cluster with pre-flight validation.

    No live code path in this app calls this function anymore (2026-07-18):
    its last caller, the ``cluster-admin-review`` gate's approval path
    (``routes/gates.py``, for CI/CD manifests destined for a shared operator
    namespace), was retired the same day that category started delivering
    via a GitOps PR instead -- see ``portal/delivery.py``'s
    ``CATEGORY_CICD_SHARED_NAMESPACE`` handling and the README. AgentIT no
    longer performs a direct cluster apply for any delivery category, full
    stop. This function (and ``allow_operator_namespaces`` -- see
    ``_classify_and_fix``) is kept, unreachable, rather than deleted: it's a
    real, well-tested, general "apply YAML manifests to a cluster safely"
    primitive, not a delivery-mechanism-specific hack -- see
    ``MECHANISM_DIRECT_APPLY``'s identical "kept but never selected"
    precedent in ``delivery.py``.

    Conflicts (a genuine server-side-apply field-manager conflict, HTTP 409)
    are collected into the returned dict's ``conflicts`` list, kept separate
    from ``errors`` so callers can tell "another manager owns this field"
    apart from an ordinary apply failure. There is no longer a ``force``
    parameter to seize ownership on a conflict -- that existed only for the
    ``cluster-conflict-review`` gate type's force-reapply, which has been
    removed along with Direct Apply as a concept entirely (a conflict here
    now just surfaces in ``conflicts``, same as any other unresolved
    failure, with no automatic or human-triggered force-through path).

    ``dry_run`` is threaded straight through to ``kube.apply_yaml()``'s own
    ``dry_run`` parameter -- a real server-side-apply dry run against the
    cluster's API server (missing CRDs, RBAC denials, schema/admission-
    webhook rejections, quota, ...) rather than only checking that a
    manifest has a recognizable ``kind``. An unreachable cluster (or any
    other apply failure) surfaces as a normal per-manifest entry in
    ``errors``/``conflicts`` exactly like the real-apply path, never as a
    false "OK".
    """
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    conflicts: list[dict] = []
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
            action, reason, fixed = _classify_and_fix(
                doc, namespace, available, allow_operator_namespaces=allow_operator_namespaces,
            )
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

        result = kube.apply_yaml(content, namespace, dry_run=dry_run)
        log_prefix = "Dry-run:" if dry_run else "Real apply:"
        if result["applied"]:
            applied.append(fpath)
            logger.info("%s %s applied cleanly", log_prefix, fpath)
        elif result.get("conflict"):
            conflicts.append({
                "path": fpath, "error": result["error"],
                "details": result.get("conflict_details", []),
            })
            logger.warning("%s field-manager conflict applying %s: %s", log_prefix, fpath, result["error"])
        else:
            errors.append(f"{fpath}: {result['error']}")
            logger.error("%s failed %s: %s", log_prefix, fpath, result["error"])

    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "conflicts": conflicts,
        "missing_operators": missing_operators,
        "repo_files": repo_files,
    }


async def apply_with_verification(
    files: list[dict],
    namespace: str,
    dry_run: bool,
    *,
    store: object,
    app_name: str,
    skill_outcome_reason: str,
    actor: str,
    action: str,
    resource: str,
    allow_operator_namespaces: bool = False,
) -> dict:
    """Shared "apply to cluster, with consistent side effects" sequence.

    Direct Apply has been removed as a concept entirely, and (2026-07-18)
    so has this function's own last caller, the ``cluster-admin-review``
    gate's approval path (``routes/gates.py``, for CI/CD manifests destined
    for a shared operator namespace -- see ``apply_manifests_to_cluster``'s
    own docstring). No live code path calls this function anymore.
    ``AutoMode`` and the manual Deliver route both go through
    ``portal/delivery.py::route_and_deliver()`` instead, which never calls
    this function at all for any category (it either commits to the GitOps
    infra repo or refuses outright). Kept, unreachable, for the same reason
    ``apply_manifests_to_cluster()`` is -- see that function's docstring.

    This previously had a second caller-selectable behavior
    (``force_dry_run_first``, ``AutoMode``'s own always-dry-run-first safety
    net) and a ``record_outcomes_on_partial_failure``/``force`` knob, all
    removed along with Direct Apply/``AutoMode``'s direct-apply branch as a
    concept entirely -- with one real caller left, there's nothing left to
    keep those knobs distinguishing between.

    ``allow_operator_namespaces`` -- see ``apply_manifests_to_cluster`` --
    defaults to ``False`` and, when left at that default, is never passed
    through to ``apply_manifests_to_cluster`` at all (not even as
    ``allow_operator_namespaces=False``), so a caller's calls to the mocked
    ``apply_manifests_to_cluster`` in tests stay byte-for-byte identical to
    before this parameter existed.

    Side effects:

    - ``record_skill_outcomes()`` fires after a real apply (never after a
      dry-run-only call) that produced at least one applied file, even if
      other files in the same batch errored (matching the manual route's
      pre-existing "record whatever actually applied" behavior).
    - ``audit_log()`` fires exactly once per call, covering every exit path
      (dry-run-only, real apply, an unexpected exception).

    Returns ``apply_manifests_to_cluster()``'s result dict (including its
    ``conflicts`` list -- see that function's docstring), plus
    ``is_dry_run``: whether ``applied``/``skipped``/``errors`` reflect a
    dry-run (``True``) or a real apply (``False``).
    """
    extra_kwargs = {"allow_operator_namespaces": True} if allow_operator_namespaces else {}
    try:
        result = await asyncio.to_thread(apply_manifests_to_cluster, files, namespace, dry_run, **extra_kwargs)
    except Exception:
        audit_log(
            actor=actor, action=action, resource=resource, outcome="error",
            details={"namespace": namespace, "dry_run": dry_run},
        )
        raise

    if not dry_run:
        await record_skill_outcomes(
            store, app_name, files, set(result["applied"]), "approved", skill_outcome_reason,
        )

    if not result["errors"] and not result.get("conflicts"):
        outcome = "success"
    elif result.get("conflicts") and not result["errors"]:
        outcome = "conflict"
    else:
        outcome = "partial"
    audit_log(
        actor=actor, action=action, resource=resource,
        outcome=outcome,
        details={
            "namespace": namespace, "dry_run": dry_run,
            "applied": len(result["applied"]), "errors": len(result["errors"]),
            "conflicts": len(result.get("conflicts", [])),
        },
    )

    return {**result, "is_dry_run": dry_run}


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
