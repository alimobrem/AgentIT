"""Fix registry — maps finding categories to solution-complete skills.

Historically this mapped finding categories directly to Python agent
generator methods (agents/hardening.py, compliance.py, cicd.py,
observability.py). Those agents were removed once skills gained full
template-fallback parity for their domains (see
docs/agent-removal-readiness.md) -- the registry now maps a category to a
(domain, skill_name) pair that RemediationDispatcher resolves via
SkillEngine instead of an agent class/method.

**Solution contracts** (2026-07-22, #154 + hardening): each registered
finding declares delivery surface (``source`` / ``cluster`` / ``none``),
``auto_pr``, human-readable clear evidence, machine ``evidence_kind`` for
pre-open simulation, and refuse companions. Scan must open only the
clearing surface's PR — never a wrong-layer companion. See
``portal/quality_prs.py``, ``remediation/clear_evidence.py``, and
``SkillEngine.match()``.

**Fleet vs self-managed:** ``delivery: cluster`` lands in agentit-gitops
``apps/{app}/…`` for fleet apps, and in the app repo ``chart/`` (source PR)
for self-managed AgentIT. ``delivery: source`` always patches the app repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentit.remediation import clear_evidence as _ev


@dataclass(frozen=True)
class SolutionContract:
    """How a finding is actually cleared — skill + delivery + evidence."""

    domain: str
    skill_name: str
    # ``source`` → app-repo patch; ``cluster`` → gitops / chart manifests;
    # ``none`` → detect_only / no Scan PR.
    delivery: str
    # One-line honesty for PR bodies: "Clears finding X by doing Y".
    clear_evidence: str
    # Machine evidence kind for pre-open simulation (clear_evidence.py).
    evidence_kind: str
    # Extra params for evidence (e.g. K8s kinds for cluster_kind).
    evidence_params: frozenset[str] = field(default_factory=frozenset)
    # Skills that keyword-overlap this finding but never clear it.
    refuse_companions: frozenset[str] = field(default_factory=frozenset)
    # False → detect_only / no_auto_pr: Scan must not open a PR.
    auto_pr: bool = True
    # Where cluster delivery lands — documented for humans + portal.
    # Fleet always uses apps/{app}/; self-managed uses chart/ via source PR.
    fleet_path: str = "apps/{app}/"
    self_managed_path: str = "chart/"


def _c(
    domain: str,
    skill_name: str,
    delivery: str,
    clear_evidence: str,
    evidence_kind: str,
    *refuse: str,
    auto_pr: bool = True,
    kinds: tuple[str, ...] = (),
    fleet_path: str = "apps/{app}/",
    self_managed_path: str = "chart/",
) -> SolutionContract:
    return SolutionContract(
        domain=domain,
        skill_name=skill_name,
        delivery=delivery,
        clear_evidence=clear_evidence,
        evidence_kind=evidence_kind,
        evidence_params=frozenset(kinds),
        refuse_companions=frozenset(refuse),
        auto_pr=auto_pr,
        fleet_path=fleet_path if delivery == "cluster" else "",
        self_managed_path=(
            self_managed_path if delivery == "cluster"
            else ("." if delivery == "source" else "")
        ),
    )


# Authoritative per-finding solution contracts. FIX_REGISTRY below is derived
# from auto_pr rows so RemediationDispatcher / skill_for_category keep a
# stable (domain, skill) API for remediable findings only.
SOLUTION_CONTRACTS: dict[str, SolutionContract] = {
    "container": _c(
        "security", "containerfile", "source",
        "pinning the app Dockerfile/Containerfile base image (no :latest)",
        _ev.DOCKERFILE_PIN,
        "image-registry-policy", "limitrange", "image-scan-task",
        "kyverno-require-labels",
    ),
    "dockerfile": _c(
        "security", "containerfile", "source",
        "pinning the app Dockerfile/Containerfile base image (no :latest)",
        _ev.DOCKERFILE_PIN,
        "image-registry-policy", "limitrange", "image-scan-task",
        "kyverno-require-labels",
    ),
    "network": _c(
        "security", "network-policy", "cluster",
        "applying a NetworkPolicy that isolates the workload",
        _ev.CLUSTER_KIND,
        kinds=("NetworkPolicy",),
    ),
    "scanning": _c(
        "security", "image-scan-task", "cluster",
        "adding a Tekton image-scan Task referenced by the pipeline",
        _ev.CLUSTER_KIND,
        kinds=("Task", "ClusterTask"),
    ),
    "vulnerability": _c(
        "security", "image-scan-task", "cluster",
        "adding a Tekton image-scan Task referenced by the pipeline",
        _ev.CLUSTER_KIND,
        kinds=("Task", "ClusterTask"),
    ),
    "cve": _c(
        "security", "image-scan-task", "cluster",
        "adding a Tekton image-scan Task referenced by the pipeline",
        _ev.CLUSTER_KIND,
        kinds=("Task", "ClusterTask"),
    ),
    "resource": _c(
        "security", "resource-limits", "cluster",
        "setting container resource requests/limits",
        _ev.RESOURCE_LIMITS,
    ),
    # Analyzer emits plural ``resources`` (infrastructure.py).
    "resources": _c(
        "security", "resource-limits", "cluster",
        "setting container resource requests/limits",
        _ev.RESOURCE_LIMITS,
    ),
    # Non-skill sentinel — RemediationDispatcher special-cases patch_base_image.
    "base_image": _c(
        "security", "patch_base_image", "source",
        "patching the base image reference in source",
        _ev.BASE_IMAGE_PIN,
    ),
    "policy": _c(
        "compliance", "kyverno-require-labels", "cluster",
        "enforcing required labels via a Kyverno Policy",
        _ev.CLUSTER_KIND,
        "image-registry-policy",
        kinds=("Policy", "ClusterPolicy"),
    ),
    "sbom": _c(
        "compliance", "sbom-task", "cluster",
        "adding a Tekton SBOM Task",
        _ev.CLUSTER_KIND,
        kinds=("Task", "ClusterTask"),
    ),
    # App-level audit logging (compliance analyzer). Cluster apiserver
    # audit-policy ConfigMap does NOT clear this finding.
    "audit": _c(
        "compliance", "app-audit-logging", "source",
        "wiring an app audit module into the API entrypoint (import + middleware)",
        _ev.AUDIT_WIRED,
        "audit-policy",
    ),
    "pipeline": _c(
        "cicd", "tekton-pipeline", "cluster",
        "adding a Tekton Pipeline/PipelineRun for the app",
        _ev.CLUSTER_KIND,
        kinds=("Pipeline", "PipelineRun"),
    ),
    "gitops": _c(
        "cicd", "argocd-application", "cluster",
        "registering an Argo CD Application for the app",
        _ev.CLUSTER_KIND,
        kinds=("Application", "ApplicationSet"),
    ),
    "metrics": _c(
        "observability", "service-monitor", "cluster",
        "adding a ServiceMonitor for Prometheus scraping",
        _ev.CLUSTER_KIND,
        kinds=("ServiceMonitor",),
    ),
    "tracing": _c(
        "observability", "otel-collector", "cluster",
        "adding OpenTelemetry collector config for the app",
        _ev.CLUSTER_KIND,
        kinds=("OpenTelemetryCollector", "ConfigMap", "Deployment"),
    ),
    "dashboards": _c(
        "observability", "grafana-dashboard", "cluster",
        "adding a Grafana dashboard ConfigMap for RED metrics",
        _ev.CLUSTER_KIND,
        kinds=("ConfigMap",),
    ),
    "alerting": _c(
        "observability", "alerting-rules", "cluster",
        "adding PrometheusRule alerting rules for the app",
        _ev.CLUSTER_KIND,
        kinds=("PrometheusRule",),
    ),
    "rbac": _c(
        "security", "rbac", "cluster",
        "adding a ServiceAccount/Role/RoleBinding for the workload",
        _ev.CLUSTER_KIND,
        kinds=("ServiceAccount", "Role", "RoleBinding", "ClusterRoleBinding"),
    ),
    "autoscaling": _c(
        "infrastructure", "hpa", "cluster",
        "adding an HPA whose scaleTargetRef resolves to a live workload",
        _ev.HPA_TARGET,
    ),
    "monitoring": _c(
        "observability", "service-monitor", "cluster",
        "adding a ServiceMonitor for Prometheus scraping",
        _ev.CLUSTER_KIND,
        kinds=("ServiceMonitor",),
    ),
    "scaling": _c(
        "infrastructure", "hpa", "cluster",
        "adding an HPA whose scaleTargetRef resolves to a live Deployment/Rollout",
        _ev.HPA_TARGET,
    ),
    "quota": _c(
        "infrastructure", "resourcequota", "cluster",
        "adding a ResourceQuota/LimitRange in the app namespace",
        _ev.QUOTA_MANIFEST,
    ),
    "availability": _c(
        "infrastructure", "pdb", "cluster",
        "adding a PodDisruptionBudget for the workload",
        _ev.CLUSTER_KIND,
        "pod-delete",
        kinds=("PodDisruptionBudget",),
    ),
    "eol": _c(
        "infrastructure", "eol-upgrade", "source",
        "bumping the runtime pin (.node-version / .python-version) in the app repo",
        _ev.RUNTIME_PIN,
    ),
    "migration": _c(
        "data_governance", "db-migration-tooling", "source",
        "scaffolding real Alembic/SQL migrations (revision + env URL; "
        "not target_metadata=None theater); hand-rolled store DDL already passes",
        _ev.MIGRATION_TOOLING,
    ),
    "iac": _c(
        "infrastructure", "helm-chart", "source",
        "adding a real Helm chart (Chart.yaml + templates) in the app repo",
        _ev.HELM_CHART,
    ),
    "manifests": _c(
        "infrastructure", "helm-chart", "source",
        "adding Helm templates with apiVersion/kind in the app repo",
        _ev.HELM_CHART,
    ),
    "health": _c(
        "infrastructure", "health-probes-policy", "cluster",
        "adding a Kyverno mutate Policy that injects liveness/readiness probes",
        _ev.CLUSTER_KIND,
        kinds=("Policy", "ClusterPolicy"),
    ),
    # --- detect_only / no_auto_pr (mode:detect skills or human-only) ---
    "license": _c(
        "compliance", "license-file-exists", "none",
        "detect-only: LICENSE* presence — human adds license text (no auto-PR)",
        _ev.DETECT_ONLY, auto_pr=False,
    ),
    "backup": _c(
        "data_governance", "backup-config-exists", "none",
        "detect-only: backup CronJob/Crunchy schedule — human designs backup (no auto-PR)",
        _ev.DETECT_ONLY, auto_pr=False,
    ),
    "retention": _c(
        "data_governance", "retention-policy-exists", "none",
        "detect-only: retention policy doc/config — human authors policy (no auto-PR)",
        _ev.DETECT_ONLY, auto_pr=False,
    ),
    "logging": _c(
        "observability", "structured-logging-detected", "none",
        "detect-only: structured logging library — human wires logging (no auto-PR)",
        _ev.DETECT_ONLY, auto_pr=False,
    ),
    "instrumentation": _c(
        "observability", "structured-logging-detected", "none",
        "detect-only: OpenTelemetry SDK in app source — human instruments (no auto-PR)",
        _ev.DETECT_ONLY, auto_pr=False,
    ),
    "secrets": _c(
        "security", "__detect_only_secrets__", "none",
        "detect-only: rotate leaked secret + ExternalSecrets/Vault — human-only (no auto-PR)",
        _ev.DETECT_ONLY, auto_pr=False,
    ),
}


# Remediate-only (domain, skill_name) map — detect_only rows stay in
# SOLUTION_CONTRACTS but not FIX_REGISTRY.
FIX_REGISTRY: dict[str, tuple[str, str]] = {
    key: (c.domain, c.skill_name)
    for key, c in SOLUTION_CONTRACTS.items()
    if c.auto_pr
}


def _normalize(category: str) -> str:
    return (category or "").lower().replace(" ", "_").replace("-", "_")


def contract_for(category: str) -> SolutionContract | None:
    """Return the solution contract for a finding category, or None.

    Exact normalized key only. Bare substring matching falsely mapped
    multi-word fixture categories (e.g. ``cost … resources`` → ``resource``)
    and swallowed domains that have no SOLUTION_CONTRACT.
    """
    cat = _normalize(category)
    return SOLUTION_CONTRACTS.get(cat)


def allows_auto_pr(category: str) -> bool:
    """True when Scan may open a finding-clear PR for this category.

    Fail-closed: uncontracted categories and ``auto_pr=False`` never open PRs.
    """
    contract = contract_for(category)
    return contract is not None and contract.auto_pr


def remediable_findings(
    target_findings: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep only findings that have an auto_pr solution contract."""
    return [(c, d) for c, d in target_findings if c and allows_auto_pr(c)]


def lookup(category: str) -> tuple[str, str] | None:
    """Find the (domain, skill_name) for a remediable finding category."""
    contract = contract_for(category)
    if contract is None or not contract.auto_pr:
        return None
    return (contract.domain, contract.skill_name)


def clears_via_source(category: str) -> bool:
    """True when the finding only clears via an app-repo source patch."""
    contract = contract_for(category)
    return (
        contract is not None
        and contract.auto_pr
        and contract.delivery == "source"
    )


def delivery_path_hint(category: str, *, self_managed: bool) -> str:
    """Human-readable where the clearing change lands (fleet vs self-managed)."""
    contract = contract_for(category)
    if contract is None:
        return ""
    if contract.delivery == "none":
        return "(detect-only — no PR path)"
    if contract.delivery == "source":
        return "app repo (source patch)"
    if self_managed:
        return f"app repo {contract.self_managed_path or 'chart/'} (self-managed)"
    return f"gitops {contract.fleet_path or 'apps/{app}/'} (fleet)"


def expected_clear_lines(target_findings: list[tuple[str, str]]) -> list[str]:
    """PR-body lines: 'Clears `cat` by …' for each targeted finding."""
    lines: list[str] = []
    for cat, _desc in target_findings:
        contract = contract_for(cat)
        if contract is None:
            lines.append(f"Clear `{cat}` on next re-Assess.")
            continue
        if not contract.auto_pr:
            lines.append(
                f"`{cat}` is detect-only / no auto-PR — "
                f"{contract.clear_evidence}."
            )
            continue
        path_hint = ""
        if contract.delivery == "cluster":
            path_hint = (
                f" Fleet → `{contract.fleet_path}`; "
                f"self-managed → `{contract.self_managed_path}`."
            )
        elif contract.delivery == "source":
            path_hint = " App-repo source patch."
        lines.append(
            f"Clears `{cat}` by {contract.clear_evidence} "
            f"(delivery: **{contract.delivery}**, "
            f"evidence: `{contract.evidence_kind}`)."
            f"{path_hint}"
        )
    return lines
