"""Single source of truth for agent capability descriptions.

Used by the orchestrator (for agent registration) and the portal
(for display on agent pages).
"""
from __future__ import annotations

# security, observability, cicd, compliance, infrastructure, incident,
# release, retirement, and chaos are now skill-only domains -- their
# Python agents (agents/hardening.py, cicd.py, compliance.py,
# infrastructure.py, incident.py, release.py, retirement.py,
# observability.py, chaos.py) were removed once skills gained full
# template-fallback parity for every artifact they used to generate. See
# docs/agent-removal-readiness.md for the domain-by-domain readiness
# audit. `dependency` and `cost` keep their Python agents specifically for
# the narrative dependency-report.md/cost-report.md outputs, which depend
# on runtime-computed data (detected ecosystems/CVEs, computed cost tier)
# that a static skill template has no access to -- see that same doc's
# recommendation and this repo's "no mock data" rule. `codechange` is kept
# because it patches the application's own source repo, not a K8s
# manifest -- a fundamentally different capability skills don't model.
AGENT_CAPABILITIES: dict[str, str] = {
    "cost": "VPA, cost labels, cost report",
    "dependency": "Dependency report, Renovate/Dependabot config",
    "codechange": ".gitignore, OTel instrumentation, structured logging",
    # Long-lived watcher agents
    "vuln-watcher": "Monitors fleet for CVEs, raises an alert for every critical/high finding",
    "slo-tracker": "Checks SLO status, publishes breach alerts, recommends rollbacks",
    "drift-detector": "Queries Argo CD for OutOfSync apps, auto-syncs them back to Git",
    "skill-learner": "Researches CVEs via LLM, drafts new skills for human review",
    "capability-scout": "Proposes small, evidence-grounded changes to AgentIT itself as a draft PR",
    "reassess-scheduler": "Automatically re-Assesses apps on their configured cadence (daily/weekly/monthly)",
}

RESOURCE_TIERS: dict[str, dict[str, str]] = {
    "small": {"cpu_req": "50m", "cpu_lim": "250m", "mem_req": "128Mi", "mem_lim": "256Mi"},
    "standard": {"cpu_req": "100m", "cpu_lim": "500m", "mem_req": "256Mi", "mem_lim": "512Mi"},
    "large": {"cpu_req": "250m", "cpu_lim": "1000m", "mem_req": "512Mi", "mem_lim": "1Gi"},
}

AGENT_CLASSES: dict[str, tuple[str, str, str, str]] = {
    "cost": ("cost", "agentit.agents.cost", "CostOptimizationAgent", "small"),
    "dependency": ("dependency", "agentit.agents.dependency", "DependencyAgent", "small"),
    "codechange": ("codechange", "agentit.agents.codechange", "CodeChangeAgent", "large"),
}


AGENT_DISPLAY_NAMES: dict[str, str] = {
    "cost": "Cost Optimization",
    "dependency": "Dependency",
    "codechange": "Code Change",
}

WATCHER_AGENTS: list[dict[str, str]] = [
    {"name": "vuln-watcher", "mode": "Kafka consumer + polling", "interval": "6 hours", "description": "Monitors fleet for critical/high findings and raises an alert for each one"},
    {"name": "slo-tracker", "mode": "Polling", "interval": "5 minutes", "description": "Checks SLO status across all assessments, publishes breach alerts, recommends rollbacks"},
    {"name": "drift-detector", "mode": "Argo CD polling", "interval": "10 minutes", "description": "Queries Argo CD apps for OutOfSync state and auto-syncs them back to the Git-declared state"},
    {"name": "skill-learner", "mode": "LLM polling", "interval": "24 hours", "description": "Researches recent CVEs via LLM and drafts new skills (status: draft) for human review — requires an LLM connection"},
    {"name": "capability-scout", "mode": "LLM polling", "interval": "24 hours", "description": "Reads fleet usage/effectiveness data and doc-gap signals, proposes one small change to AgentIT itself as a draft PR for human review — requires an LLM connection and GITHUB_TOKEN"},
    {"name": "reassess-scheduler", "mode": "Polling", "interval": "1 hour", "description": "Checks every app's configured re-assessment cadence (daily/weekly/monthly, set on its Assessment Detail page) and automatically re-Assesses any app that's due, via the same route the manual Scan/Re-scan button uses"},
]


def get_onboarding_agents() -> list[dict[str, str]]:
    return [
        {"name": AGENT_DISPLAY_NAMES[cat], "generates": AGENT_CAPABILITIES[cat], "category": cat}
        for cat in AGENT_CLASSES
    ]


def get_agent_class(name: str):
    """Lazy-import and return the agent class for the given name."""
    import importlib
    if name not in AGENT_CLASSES:
        raise ValueError(f"Unknown agent: {name}")
    _cat, module_path, class_name, _tier = AGENT_CLASSES[name]
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)
