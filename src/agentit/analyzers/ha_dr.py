from __future__ import annotations

from pathlib import Path

import yaml

from agentit.analyzers.base import calculate_score, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity

# The same workload-kind set property_verifier.py's own workload-container
# probe already treats as "a real workload" -- reused here so both analyzers
# agree on what counts as a live-shaped resource, not two independently
# drifting lists.
_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "Rollout", "DaemonSet")


def _workload_replica_counts(content: str) -> list[int]:
    """Yield ``spec.replicas`` from every real Deployment/StatefulSet/
    Rollout/DaemonSet document in ``content`` -- never a bare regex match
    against raw file text.

    Confirmed live (documented, previously unfixed): a plain
    ``re.search(r"replicas:\\s*...")`` over the whole file matched
    ``replicas:`` anywhere at all -- inside a comment, an unrelated
    resource, or a completely disconnected stub manifest the app doesn't
    actually use -- and silently cleared the finding. Scoping to an actual
    workload-kind document's own ``spec.replicas`` field closes that class
    of false-clear (a comment or unrelated field can never masquerade as a
    workload's real replica count). This is still repo-only static
    analysis with no live-cluster correlation, so a *fully-formed but
    genuinely orphaned* Deployment YAML sitting unused in the repo can
    still false-clear -- that would need the same live workload-discovery
    access ``fleet_hpa.discover_namespace_workloads()`` already has, which
    this analyzer (no cluster context, repo path only) does not.
    """
    try:
        docs = yaml.safe_load_all(content)
        parsed = list(docs)
    except yaml.YAMLError:
        return []
    counts: list[int] = []
    for doc in parsed:
        if not isinstance(doc, dict) or doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            continue
        try:
            counts.append(int(spec.get("replicas")))
        except (TypeError, ValueError):
            continue
    return counts


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
            if any(count >= 2 for count in _workload_replica_counts(content)):
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
