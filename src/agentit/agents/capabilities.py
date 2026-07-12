"""Single source of truth for agent capability descriptions.

Used by the orchestrator (for agent registration) and the portal
(for display on agent pages).
"""
from __future__ import annotations

AGENT_CAPABILITIES: dict[str, str] = {
    "security": "NetworkPolicy, Containerfile, RBAC, SCCs, resource limits, image scan task",
    "observability": "ServiceMonitor, Grafana dashboard, alerting rules, OTel collector",
    "cicd": "Tekton Pipeline (scan + SBOM), Argo CD Application, Argo Rollout, Containerfile",
    "compliance": "Kyverno policies, SBOM task, audit policy, compliance evidence",
    "infrastructure": "HPA, PDB, ResourceQuota, LimitRange, Namespace",
    "cost": "VPA, cost labels, cost report",
    "dependency": "Dependency report, Renovate/Dependabot config",
    "incident": "Runbook, PagerDuty config, Alertmanager config",
    "release": "AnalysisTemplate, Rollout patch, rollback policy",
    "codechange": ".gitignore, OTel instrumentation, structured logging",
    "retirement": "Decommission plan, cleanup task, data archive job",
    # Long-lived watcher agents
    "vuln-watcher": "Monitors fleet for CVEs, triggers remediation when auto-mode on",
    "slo-tracker": "Checks SLO status, publishes breach alerts, recommends rollbacks",
    "drift-detector": "Queries Argo CD for OutOfSync apps, optionally auto-syncs",
}

RESOURCE_TIERS: dict[str, dict[str, str]] = {
    "small": {"cpu_req": "50m", "cpu_lim": "250m", "mem_req": "128Mi", "mem_lim": "256Mi"},
    "standard": {"cpu_req": "100m", "cpu_lim": "500m", "mem_req": "256Mi", "mem_lim": "512Mi"},
    "large": {"cpu_req": "250m", "cpu_lim": "1000m", "mem_req": "512Mi", "mem_lim": "1Gi"},
}

AGENT_CLASSES: dict[str, tuple[str, str, str, str]] = {
    "security": ("security", "agentit.agents.hardening", "HardeningAgent", "standard"),
    "observability": ("observability", "agentit.agents.observability", "ObservabilityAgent", "small"),
    "cicd": ("cicd", "agentit.agents.cicd", "CICDAgent", "standard"),
    "compliance": ("compliance", "agentit.agents.compliance", "ComplianceAgent", "small"),
    "infrastructure": ("infrastructure", "agentit.agents.infrastructure", "InfrastructureAgent", "small"),
    "cost": ("cost", "agentit.agents.cost", "CostOptimizationAgent", "small"),
    "dependency": ("dependency", "agentit.agents.dependency", "DependencyAgent", "small"),
    "incident": ("incident", "agentit.agents.incident", "IncidentAgent", "small"),
    "release": ("release", "agentit.agents.release", "ReleaseCoordinatorAgent", "small"),
    "retirement": ("retirement", "agentit.agents.retirement", "RetirementAgent", "small"),
    "codechange": ("codechange", "agentit.agents.codechange", "CodeChangeAgent", "large"),
}


def get_agent_class(name: str):
    """Lazy-import and return the agent class for the given name."""
    import importlib
    if name not in AGENT_CLASSES:
        raise ValueError(f"Unknown agent: {name}")
    _cat, module_path, class_name, _tier = AGENT_CLASSES[name]
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)
