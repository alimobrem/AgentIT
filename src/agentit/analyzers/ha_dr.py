from __future__ import annotations

import re
from pathlib import Path

from agentit.models import DimensionScore, Finding, Severity

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "dist", "build", "target"}


class HADRAnalyzer:
    dimension = "ha_dr"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_replicas = False
        has_pdb = False
        has_hpa = False
        has_health_probes = False
        has_anti_affinity = False

        for yaml_file in list(repo_path.rglob("*.yaml")) + list(repo_path.rglob("*.yml")):
            if any(d in yaml_file.relative_to(repo_path).parts for d in IGNORED_DIRS):
                continue
            try:
                content = yaml_file.read_text(errors="ignore")
            except OSError:
                continue

            if re.search(r"replicas:\s*[2-9]", content):
                has_replicas = True
            if "PodDisruptionBudget" in content:
                has_pdb = True
            if "HorizontalPodAutoscaler" in content:
                has_hpa = True
            if "livenessProbe" in content or "readinessProbe" in content:
                has_health_probes = True
            if "podAntiAffinity" in content:
                has_anti_affinity = True

        if not has_replicas:
            findings.append(Finding(
                category="availability",
                severity=Severity.high,
                description="Single replica or no replica count defined -- no redundancy",
                recommendation="Set replicas >= 2 for high availability",
            ))
        if not has_pdb:
            findings.append(Finding(
                category="availability",
                severity=Severity.medium,
                description="No PodDisruptionBudget defined",
                recommendation="Add PDB to prevent all pods being evicted during maintenance",
            ))
        if not has_hpa:
            findings.append(Finding(
                category="scaling",
                severity=Severity.medium,
                description="No HorizontalPodAutoscaler defined",
                recommendation="Add HPA for automatic scaling under load",
            ))
        if not has_health_probes:
            findings.append(Finding(
                category="health",
                severity=Severity.high,
                description="No liveness or readiness probes defined",
                recommendation="Add livenessProbe and readinessProbe to all containers",
            ))

        score = 100
        for f in findings:
            if f.severity == Severity.high:
                score -= 25
            elif f.severity == Severity.medium:
                score -= 12
        return DimensionScore(dimension="ha_dr", score=max(0, score), max_score=100, findings=findings)
