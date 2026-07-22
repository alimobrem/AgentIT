"""Fix registry — maps finding categories to solution-complete skills.

Historically this mapped finding categories directly to Python agent
generator methods (agents/hardening.py, compliance.py, cicd.py,
observability.py). Those agents were removed once skills gained full
template-fallback parity for their domains (see
docs/agent-removal-readiness.md) -- the registry now maps a category to a
(domain, skill_name) pair that RemediationDispatcher resolves via
SkillEngine instead of an agent class/method.

**Solution contracts** (2026-07-22): each registered finding also declares
the delivery surface that actually clears it (``source`` vs ``cluster``
vs ``none`` for detect_only) and human-readable clear evidence. Scan must
open only that surface's PR — never a wrong-layer companion (e.g. Kyverno
for a Dockerfile ``:latest`` finding, or apiserver ``audit-policy`` for
app audit logging). See ``portal/quality_prs.py`` and ``SkillEngine.match()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SolutionContract:
    """How a finding is actually cleared — skill + delivery + evidence."""

    domain: str
    skill_name: str
    # ``source`` → app-repo patch (CATEGORY_SOURCE_PATCH); ``cluster`` →
    # gitops / chart manifests; ``none`` → detect_only / no Scan PR.
    # Companion PRs on the wrong surface do not clear the finding and must
    # not be opened for "finding-clear".
    delivery: str
    # One-line honesty for PR bodies: "Clears finding X by doing Y".
    clear_evidence: str
    # Skills that keyword-overlap this finding but never clear it (wrong
    # layer). Match + quality filter refuse these as companions.
    refuse_companions: frozenset[str] = field(default_factory=frozenset)
    # False → detect_only / no_auto_pr: Scan must not open a PR for this
    # category (mode:detect skill, or human-only remediation like secrets).
    auto_pr: bool = True


def _c(
    domain: str,
    skill_name: str,
    delivery: str,
    clear_evidence: str,
    *refuse: str,
    auto_pr: bool = True,
) -> SolutionContract:
    return SolutionContract(
        domain=domain,
        skill_name=skill_name,
        delivery=delivery,
        clear_evidence=clear_evidence,
        refuse_companions=frozenset(refuse),
        auto_pr=auto_pr,
    )


# Authoritative per-finding solution contracts. FIX_REGISTRY below is derived
# from auto_pr rows so RemediationDispatcher / skill_for_category keep a
# stable (domain, skill) API for remediable findings only.
SOLUTION_CONTRACTS: dict[str, SolutionContract] = {
    "container": _c(
        "security", "containerfile", "source",
        "pinning the app Dockerfile/Containerfile base image (no :latest)",
        "image-registry-policy", "limitrange", "image-scan-task",
        "kyverno-require-labels",
    ),
    "dockerfile": _c(
        "security", "containerfile", "source",
        "pinning the app Dockerfile/Containerfile base image (no :latest)",
        "image-registry-policy", "limitrange", "image-scan-task",
        "kyverno-require-labels",
    ),
    "network": _c(
        "security", "network-policy", "cluster",
        "applying a NetworkPolicy that isolates the workload",
    ),
    "scanning": _c(
        "security", "image-scan-task", "cluster",
        "adding a Tekton image-scan Task referenced by the pipeline",
    ),
    "vulnerability": _c(
        "security", "image-scan-task", "cluster",
        "adding a Tekton image-scan Task referenced by the pipeline",
    ),
    "cve": _c(
        "security", "image-scan-task", "cluster",
        "adding a Tekton image-scan Task referenced by the pipeline",
    ),
    "resource": _c(
        "security", "resource-limits", "cluster",
        "setting container resource requests/limits",
    ),
    # Analyzer emits plural ``resources`` (infrastructure.py); same contract.
    "resources": _c(
        "security", "resource-limits", "cluster",
        "setting container resource requests/limits",
    ),
    # Non-skill sentinel — RemediationDispatcher special-cases patch_base_image.
    "base_image": _c(
        "security", "patch_base_image", "source",
        "patching the base image reference in source",
    ),
    "policy": _c(
        "compliance", "kyverno-require-labels", "cluster",
        "enforcing required labels via a Kyverno Policy",
        "image-registry-policy",
    ),
    "sbom": _c(
        "compliance", "sbom-task", "cluster",
        "adding a Tekton SBOM Task",
    ),
    # App-level audit logging (compliance analyzer). Cluster apiserver
    # audit-policy ConfigMap does NOT clear this finding.
    "audit": _c(
        "compliance", "app-audit-logging", "source",
        "wiring an app audit module into the API entrypoint (import + middleware)",
        "audit-policy",
    ),
    "pipeline": _c(
        "cicd", "tekton-pipeline", "cluster",
        "adding a Tekton Pipeline/PipelineRun for the app",
    ),
    "gitops": _c(
        "cicd", "argocd-application", "cluster",
        "registering an Argo CD Application for the app",
    ),
    "metrics": _c(
        "observability", "service-monitor", "cluster",
        "adding a ServiceMonitor for Prometheus scraping",
    ),
    "tracing": _c(
        "observability", "otel-collector", "cluster",
        "adding OpenTelemetry collector config for the app",
    ),
    "dashboards": _c(
        "observability", "grafana-dashboard", "cluster",
        "adding a Grafana dashboard ConfigMap for RED metrics",
    ),
    "alerting": _c(
        "observability", "alerting-rules", "cluster",
        "adding PrometheusRule alerting rules for the app",
    ),
    "rbac": _c(
        "security", "rbac", "cluster",
        "adding a ServiceAccount/Role/RoleBinding for the workload",
    ),
    "autoscaling": _c(
        "infrastructure", "hpa", "cluster",
        "adding an HPA whose scaleTargetRef resolves to a live workload",
    ),
    "monitoring": _c(
        "observability", "service-monitor", "cluster",
        "adding a ServiceMonitor for Prometheus scraping",
    ),
    "scaling": _c(
        "infrastructure", "hpa", "cluster",
        "adding an HPA whose scaleTargetRef resolves to a live Deployment/Rollout",
    ),
    "quota": _c(
        "infrastructure", "resourcequota", "cluster",
        "adding a ResourceQuota/LimitRange in the app namespace",
    ),
    "availability": _c(
        "infrastructure", "pdb", "cluster",
        "adding a PodDisruptionBudget for the workload",
        "pod-delete",
    ),
    "eol": _c(
        "infrastructure", "eol-upgrade", "source",
        "bumping the runtime pin (.node-version / .python-version) in the app repo",
    ),
    "migration": _c(
        "data_governance", "db-migration-tooling", "source",
        "scaffolding Alembic/migrations tooling in the app repo",
    ),
    "iac": _c(
        "infrastructure", "helm-chart", "source",
        "adding a real Helm chart (Chart.yaml + templates) in the app repo",
    ),
    "manifests": _c(
        "infrastructure", "helm-chart", "source",
        "adding Helm templates with apiVersion/kind in the app repo",
    ),
    "health": _c(
        "infrastructure", "health-probes-policy", "cluster",
        "adding a Kyverno mutate Policy that injects liveness/readiness probes",
    ),
    # --- detect_only / no_auto_pr (mode:detect skills or human-only) ---
    # Contracted so Scan never fuzzy-attaches companions; auto_pr=False
    # means no Scan PR for the finding itself.
    "license": _c(
        "compliance", "license-file-exists", "none",
        "detect-only: LICENSE* presence — human adds license text (no auto-PR)",
        auto_pr=False,
    ),
    "backup": _c(
        "data_governance", "backup-config-exists", "none",
        "detect-only: backup CronJob/Crunchy schedule — human designs backup (no auto-PR)",
        auto_pr=False,
    ),
    "retention": _c(
        "data_governance", "retention-policy-exists", "none",
        "detect-only: retention policy doc/config — human authors policy (no auto-PR)",
        auto_pr=False,
    ),
    "logging": _c(
        "observability", "structured-logging-detected", "none",
        "detect-only: structured logging library — human wires logging (no auto-PR)",
        auto_pr=False,
    ),
    "instrumentation": _c(
        "observability", "structured-logging-detected", "none",
        "detect-only: OpenTelemetry SDK in app source — human instruments (no auto-PR)",
        auto_pr=False,
    ),
    "secrets": _c(
        "security", "human-secrets-remediation", "none",
        "detect-only: rotate leaked secret + ExternalSecrets/Vault — human-only (no auto-PR)",
        auto_pr=False,
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
    """Return the solution contract for a finding category, or None."""
    cat = _normalize(category)
    if cat in SOLUTION_CONTRACTS:
        return SOLUTION_CONTRACTS[cat]
    for key, contract in SOLUTION_CONTRACTS.items():
        if key in cat:
            return contract
    return None


def allows_auto_pr(category: str) -> bool:
    """True when Scan may open a finding-clear PR for this category.

    Fail-closed: uncontracted categories and ``auto_pr=False`` (detect_only /
    no_auto_pr) never open PRs — closes the fuzzy-companion hole for gaps.
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
        lines.append(
            f"Clears `{cat}` by {contract.clear_evidence} "
            f"(delivery: **{contract.delivery}**)."
        )
    return lines
