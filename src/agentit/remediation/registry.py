"""Fix registry — maps finding categories to skills.

Historically this mapped finding categories directly to Python agent
generator methods (agents/hardening.py, compliance.py, cicd.py,
observability.py). Those agents were removed once skills gained full
template-fallback parity for their domains (see
docs/agent-removal-readiness.md) -- the registry now maps a category to a
(domain, skill_name) pair that RemediationDispatcher resolves via
SkillEngine instead of an agent class/method.
"""

from __future__ import annotations

FIX_REGISTRY: dict[str, tuple[str, str]] = {
    "container":     ("security", "containerfile"),
    "dockerfile":    ("security", "containerfile"),
    "network":       ("security", "network-policy"),
    "scanning":      ("security", "image-scan-task"),
    "vulnerability": ("security", "image-scan-task"),
    "cve":           ("security", "image-scan-task"),
    "resource":      ("security", "resource-limits"),
    "base_image":    ("security", "patch_base_image"),
    "policy":        ("compliance", "kyverno-require-labels"),
    "sbom":          ("compliance", "sbom-task"),
    # App-level audit logging (compliance analyzer looks for audit.py / audit
    # + log in source). Cluster apiserver policy remains skills/compliance/
    # audit-policy.md (advisory ConfigMap) — it does not clear this finding.
    "audit":         ("compliance", "app-audit-logging"),
    "pipeline":      ("cicd", "tekton-pipeline"),
    "gitops":        ("cicd", "argocd-application"),
    "metrics":       ("observability", "service-monitor"),
    "tracing":       ("observability", "otel-collector"),
    # Added for auto_delivery.py's validation-fix loop, which dispatches a
    # fix by the exact category name property_verifier.py's checks report
    # (rbac/autoscaling/monitoring) -- none of the substring keys above
    # happened to match any of the three (e.g. "metrics" is not a substring
    # of "monitoring"), so RemediationDispatcher.dispatch() would have
    # failed closed with "No fix registered" for every one of them despite
    # a real matching skill existing (skills/security/rbac.md,
    # skills/infrastructure/hpa.md, skills/observability/service-monitor.md).
    "rbac":          ("security", "rbac"),
    "autoscaling":   ("infrastructure", "hpa"),
    "monitoring":    ("observability", "service-monitor"),
    # Analyzer categories used by ha_dr / infrastructure (pinky Scan open
    # findings). "scaling" is not a substring of "autoscaling", so lookup
    # previously returned None and skill_for_category fell back to trigger
    # matching. "quota" had no registry row at all (resourcequota skill
    # only matched via triggers). Keep these exact so quality_prs /
    # RemediationDispatcher / skill_for_category all agree.
    "scaling":       ("infrastructure", "hpa"),
    "quota":         ("infrastructure", "resourcequota"),
    # ha_dr's "No PodDisruptionBudget defined" finding (category
    # "availability") had no registry row, so skill_for_category() fell
    # back to trigger-keyword matching -- two skills both declare trigger
    # "availability" (this one, and skills/chaos/pod-delete.md, a
    # resiliency-test generator, not a remediation), and load_all_skills()
    # sorts by path, so "skills/chaos/" < "skills/infrastructure/"
    # alphabetically meant pod-delete silently won every time. Pin it.
    "availability":  ("infrastructure", "pdb"),
    # Source-repo remediations (CATEGORY_SOURCE_PATCH) — clear on re-Assess
    # of the app repo after merge.
    "eol":           ("infrastructure", "eol-upgrade"),
    "migration":     ("data_governance", "db-migration-tooling"),
    # infrastructure.py's "iac" (no Helm/Kustomize/Terraform) and "manifests"
    # (no K8s manifests) findings both had detection with zero remediation
    # (docs: fleet-wide audit "why does most stuff get filtered out of
    # PRs"). Both are cleared by the same skill: a real Helm chart with
    # Chart.yaml (clears "iac") whose templates carry literal
    # `apiVersion:`/`kind:` text (clears "manifests" too, per
    # infrastructure.py:32-33's plain substring check) — see
    # skills/infrastructure/helm-chart.md. Source-repo delivery
    # (CATEGORY_SOURCE_PATCH): the finding is read from the app's own repo,
    # so the fix must land there too, not in gitops.
    "iac":           ("infrastructure", "helm-chart"),
    "manifests":     ("infrastructure", "helm-chart"),
    # ha_dr.py's "health" (no liveness/readiness probes) finding had
    # detection with zero remediation. Unlike iac/manifests/quota/scaling,
    # a probe is a patch to an *existing* container spec AgentIT doesn't
    # own the source definition of — see skills/infrastructure/
    # health-probes-policy.md's docstring for why this generates a
    # namespace-scoped Kyverno mutate policy (cluster delivery) instead of
    # a source-repo Deployment patch or a fabricated gitops Deployment
    # copy. live_evidence.live_health_probes_present() closes the
    # resulting source-analyzer-never-sees-it gap the same way
    # quota/scaling already do.
    "health":        ("infrastructure", "health-probes-policy"),
}


def lookup(category: str) -> tuple[str, str] | None:
    """Find the (domain, skill_name) for a finding category."""
    cat = category.lower().replace(" ", "_").replace("-", "_")
    if cat in FIX_REGISTRY:
        return FIX_REGISTRY[cat]
    for key, val in FIX_REGISTRY.items():
        if key in cat:
            return val
    return None
