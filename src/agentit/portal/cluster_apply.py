from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

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
    "VerticalPodAutoscaler": {
        "name": "VPA (Vertical Pod Autoscaler)",
        "package": "vertical-pod-autoscaler",
        "channel": "stable",
        "source": "redhat-operators",
    },
    "ClusterPolicy": {
        "name": "Kyverno",
        "package": "kyverno",
        "channel": "stable",
        "source": "community-operators",
    },
    "PrometheusRule": {
        "name": "Cluster Monitoring (built-in)",
        "package": None,
        "note": "PrometheusRule requires the OpenShift monitoring stack (enabled by default). Check: oc get crd prometheusrules.monitoring.coreos.com",
    },
    "ServiceMonitor": {
        "name": "Cluster Monitoring (built-in)",
        "package": None,
        "note": "ServiceMonitor requires the OpenShift monitoring stack.",
    },
    "Pipeline": {
        "name": "OpenShift Pipelines (Tekton)",
        "package": "openshift-pipelines-operator-rh",
        "channel": "latest",
        "source": "redhat-operators",
    },
    "Task": {
        "name": "OpenShift Pipelines (Tekton)",
        "package": "openshift-pipelines-operator-rh",
        "channel": "latest",
        "source": "redhat-operators",
    },
    "PipelineRun": {
        "name": "OpenShift Pipelines (Tekton)",
        "package": "openshift-pipelines-operator-rh",
        "channel": "latest",
        "source": "redhat-operators",
    },
    "Rollout": {
        "name": "OpenShift GitOps (Argo Rollouts)",
        "package": "openshift-gitops-operator",
        "channel": "latest",
        "source": "redhat-operators",
    },
    "OpenTelemetryCollector": {
        "name": "Red Hat OpenTelemetry",
        "package": "opentelemetry-product",
        "channel": "stable",
        "source": "redhat-operators",
    },
    "AnalysisTemplate": {
        "name": "OpenShift GitOps (Argo Rollouts)",
        "package": "openshift-gitops-operator",
        "channel": "latest",
        "source": "redhat-operators",
    },
}

_CLUSTER_SCOPED_KINDS = frozenset({
    "ClusterRole", "ClusterRoleBinding", "ClusterPolicy",
    "ClusterCleanupPolicy", "Namespace", "CustomResourceDefinition",
    "StorageClass", "PriorityClass", "ClusterIssuer",
})


def _find_cli() -> str:
    for cmd in ("oc", "kubectl"):
        if shutil.which(cmd):
            return cmd
    raise FileNotFoundError("Neither oc nor kubectl found on PATH")


def _get_available_resources(cli: str) -> set[str]:
    """Get available API resource kinds on the cluster."""
    result = subprocess.run(
        [cli, "api-resources", "--no-headers", "-o", "name"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        logger.warning("api-resources failed, skipping pre-flight: %s", result.stderr)
        return set()
    kinds: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(".")
        if parts:
            kinds.add(parts[0].lower())
    result2 = subprocess.run(
        [cli, "api-resources", "--no-headers"],
        capture_output=True, text=True, timeout=15,
    )
    if result2.returncode == 0:
        for line in result2.stdout.splitlines():
            cols = line.split()
            if cols:
                kinds.add(cols[-1].lower())
    return kinds


def _ensure_namespace(cli: str, namespace: str, dry_run: bool) -> None:
    check = subprocess.run(
        [cli, "get", "namespace", namespace],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode != 0 and not dry_run:
        subprocess.run(
            [cli, "create", "namespace", namespace],
            capture_output=True, text=True, timeout=10,
        )
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
    cli = _find_cli()
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    missing_operators: dict[str, dict] = {}
    repo_files: list[dict] = []

    _ensure_namespace(cli, namespace, dry_run)
    available = _get_available_resources(cli)

    tmpdir = tempfile.mkdtemp(prefix="agentit-apply-")
    try:
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
            tmp_file = Path(tmpdir) / Path(fpath).name
            tmp_file.write_text(content)

            cmd = [cli, "apply", "-f", str(tmp_file), "-n", namespace]
            if dry_run:
                cmd.append("--dry-run=client")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                applied.append(fpath)
                logger.info("Applied %s: %s", fpath, result.stdout.strip())
            else:
                errors.append(f"{fpath}: {result.stderr.strip()}")
                logger.error("Failed %s: %s", fpath, result.stderr.strip())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "missing_operators": missing_operators,
        "repo_files": repo_files,
    }


def install_operator(package: str, channel: str, source: str) -> dict:
    """Install an OLM operator via Subscription CR."""
    cli = _find_cli()
    sub_yaml = yaml.dump({
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {
            "name": package,
            "namespace": "openshift-operators",
        },
        "spec": {
            "channel": channel,
            "name": package,
            "source": source,
            "sourceNamespace": "openshift-marketplace",
            "installPlanApproval": "Automatic",
        },
    })
    tmpdir = tempfile.mkdtemp(prefix="agentit-install-")
    try:
        tmp_file = Path(tmpdir) / "subscription.yaml"
        tmp_file.write_text(sub_yaml)
        result = subprocess.run(
            [cli, "apply", "-f", str(tmp_file)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Operator %s install started: %s", package, result.stdout.strip())
            return {"status": "installing", "package": package}
        else:
            logger.error("Operator %s install failed: %s", package, result.stderr.strip())
            return {"status": "error", "package": package, "error": result.stderr.strip()}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
