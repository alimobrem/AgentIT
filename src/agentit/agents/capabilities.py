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
