from __future__ import annotations

import re
from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity


class HADRAnalyzer:
    dimension = "ha_dr"

    def analyze(self, repo_path: Path) -> DimensionScore:
        from agentit.remediation.workload_patches import (
            helm_templated_replicas_key,
            values_yaml_replicas_at_least,
        )

        findings: list[Finding] = []
        has_replicas = False
        has_pdb = False
        has_hpa = False
        has_health_probes = False
        # A Helm-templated workload's ``replicas:`` line is never a literal
        # digit (``{{ .Values.replicaCount }}``) — collect the referenced
        # values keys and every values.yaml's content so a real chart
        # replica count set via ``values.yaml`` (the idiomatic Helm fix)
        # clears this finding, instead of it staying open forever because
        # the raw template text never contains the number itself.
        templated_replica_keys: list[str] = []
        values_yaml_texts: list[str] = []

        for path, content in iter_yaml_files(repo_path):
            if re.search(r"replicas:\s*([2-9]|\d{2,})", content):
                has_replicas = True
            helm_key = helm_templated_replicas_key(content)
            if helm_key is not None:
                templated_replica_keys.append(helm_key)
            if path.name in ("values.yaml", "values.yml"):
                values_yaml_texts.append(content)
            if "PodDisruptionBudget" in content:
                has_pdb = True
            if "HorizontalPodAutoscaler" in content:
                has_hpa = True
            if "livenessProbe" in content or "readinessProbe" in content:
                has_health_probes = True

        if not has_replicas and templated_replica_keys:
            has_replicas = any(
                values_yaml_replicas_at_least(text) for text in values_yaml_texts
            )

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
