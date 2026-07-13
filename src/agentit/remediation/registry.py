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
    "audit":         ("compliance", "audit-policy"),
    "pipeline":      ("cicd", "tekton-pipeline"),
    "gitops":        ("cicd", "argocd-application"),
    "metrics":       ("observability", "service-monitor"),
    "tracing":       ("observability", "otel-collector"),
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
