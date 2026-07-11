"""Fix registry — maps finding categories to agent generators."""

from __future__ import annotations

FIX_REGISTRY: dict[str, tuple[str, str]] = {
    "container":     ("hardening",    "_generate_containerfile"),
    "dockerfile":    ("hardening",    "_generate_containerfile"),
    "network":       ("hardening",    "_generate_network_policy"),
    "scanning":      ("hardening",    "_generate_image_scan_task"),
    "vulnerability": ("hardening",    "_generate_image_scan_task"),
    "cve":           ("hardening",    "_generate_image_scan_task"),
    "resource":      ("hardening",    "_generate_resource_limits"),
    "base_image":    ("hardening",    "patch_base_image"),
    "policy":        ("compliance",   "_generate_kyverno_policies"),
    "sbom":          ("compliance",   "_generate_sbom_task"),
    "audit":         ("compliance",   "_generate_audit_policy"),
    "pipeline":      ("cicd",         "_generate_tekton_pipeline"),
    "gitops":        ("cicd",         "_generate_argocd_application"),
    "metrics":       ("observability", "_generate_service_monitor"),
    "tracing":       ("observability", "_generate_otel_collector"),
}


def get_agent_class(agent_key: str):
    """Lazy-import agent classes to avoid circular imports."""
    if agent_key == "hardening":
        from agentit.agents.hardening import HardeningAgent
        return HardeningAgent
    if agent_key == "compliance":
        from agentit.agents.compliance import ComplianceAgent
        return ComplianceAgent
    if agent_key == "cicd":
        from agentit.agents.cicd import CICDAgent
        return CICDAgent
    if agent_key == "observability":
        from agentit.agents.observability import ObservabilityAgent
        return ObservabilityAgent
    raise ValueError(f"Unknown agent key: {agent_key}")


def lookup(category: str) -> tuple[str, str] | None:
    """Find the agent and method for a finding category."""
    cat = category.lower().replace(" ", "_").replace("-", "_")
    if cat in FIX_REGISTRY:
        return FIX_REGISTRY[cat]
    for key, val in FIX_REGISTRY.items():
        if key in cat:
            return val
    return None
