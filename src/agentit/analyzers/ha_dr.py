from __future__ import annotations

import re
from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity


class HADRAnalyzer:
    dimension = "ha_dr"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_replicas = False
        has_pdb = False
        has_hpa = False
        has_health_probes = False

        for _, content in iter_yaml_files(repo_path):
            if re.search(r"replicas:\s*([2-9]|\d{2,})", content):
                has_replicas = True
            if "PodDisruptionBudget" in content:
                has_pdb = True
            if "HorizontalPodAutoscaler" in content:
                has_hpa = True
            if "livenessProbe" in content or "readinessProbe" in content:
                has_health_probes = True

        if not has_replicas:
            findings.append(Finding(
                category="replicas",
                severity=Severity.high,
                description="Single replica or no replica count defined -- no redundancy",
                recommendation="Set replicas >= 2 for high availability",
                source="analyzer:ha_dr",
            ))
        if not has_pdb:
            findings.append(Finding(
                category="availability",
                severity=Severity.medium,
                description="No PodDisruptionBudget defined",
                recommendation="Add PDB to prevent all pods being evicted during maintenance",
                source="analyzer:ha_dr",
            ))
        if not has_hpa:
            findings.append(Finding(
                category="scaling",
                severity=Severity.medium,
                description="No HorizontalPodAutoscaler defined",
                recommendation="Add HPA for automatic scaling under load",
                source="analyzer:ha_dr",
            ))
        if not has_health_probes:
            findings.append(Finding(
                category="health",
                severity=Severity.high,
                description="No liveness or readiness probes defined",
                recommendation="Add livenessProbe and readinessProbe to all containers",
                source="analyzer:ha_dr",
            ))

        return DimensionScore(
            dimension="ha_dr",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
