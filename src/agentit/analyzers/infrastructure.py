from __future__ import annotations

from pathlib import Path

from agentit.analyzers import eol as eol_detector
from agentit.analyzers.base import calculate_score, iter_yaml_files
from agentit.analyzers.stack_detector import StackDetector
from agentit.models import DimensionScore, Finding, Severity


class InfrastructureAnalyzer:
    dimension = "infrastructure"

    def __init__(self, llm_client: object | None = None) -> None:
        self._llm = llm_client

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        findings.extend(self._check_eol(repo_path))
        has_helm = False
        has_kustomize = False
        has_k8s_manifests = False
        has_resource_limits = False
        has_quota = False

        for path, content in iter_yaml_files(repo_path):
            name = path.name.lower()
            if name == "chart.yaml":
                has_helm = True
            if name in ("kustomization.yaml", "kustomization.yml"):
                has_kustomize = True
            if "apiVersion:" in content and "kind:" in content:
                has_k8s_manifests = True
            if "resources:" in content and ("limits:" in content or "requests:" in content):
                has_resource_limits = True
            if "kind: ResourceQuota" in content or "kind: LimitRange" in content:
                has_quota = True

        has_terraform = any(repo_path.rglob("*.tf"))

        if not has_helm and not has_kustomize and not has_terraform:
            findings.append(Finding(
                category="iac",
                severity=Severity.high,
                description="No IaC tooling detected (no Helm chart, Kustomize, or Terraform)",
                recommendation="Generate Helm chart with values.yaml and environment overlays",
                source="analyzer:infrastructure",
            ))
        if not has_k8s_manifests and not has_helm:
            findings.append(Finding(
                category="manifests",
                severity=Severity.high,
                description="No Kubernetes manifests found",
                recommendation="Create deployment, service, and ingress manifests",
                source="analyzer:infrastructure",
            ))
        if has_k8s_manifests and not has_resource_limits:
            findings.append(Finding(
                category="resources",
                severity=Severity.medium,
                description="No resource limits/requests defined in manifests",
                recommendation="Add CPU and memory requests/limits to all containers",
                source="analyzer:infrastructure",
            ))
        if has_k8s_manifests and not has_quota:
            findings.append(Finding(
                category="quota",
                severity=Severity.low,
                description="No ResourceQuota or LimitRange defined",
                recommendation="Add ResourceQuota and LimitRange for namespace governance",
                source="analyzer:infrastructure",
            ))

        return DimensionScore(
            dimension="infrastructure",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )

    def _check_eol(self, repo_path: Path) -> list[Finding]:
        """Baseline (deterministic) EOL findings, plus additional LLM-found
        risks when an LLM client is configured. The LLM path is purely
        additive and never removes/overrides a baseline finding -- see
        agentit.analyzers.eol module docstring."""
        findings = eol_detector.baseline_findings(repo_path)
        if self._llm is not None:
            stack_info = StackDetector().detect(repo_path).model_dump()
            llm_extra = eol_detector.llm_findings(repo_path, self._llm, stack_info)
            if llm_extra:
                existing_keys = {(f.category, f.description) for f in findings}
                findings.extend(f for f in llm_extra if (f.category, f.description) not in existing_keys)
        return findings
