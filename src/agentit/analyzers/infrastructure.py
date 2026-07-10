from __future__ import annotations

from pathlib import Path

from agentit.models import DimensionScore, Finding, Severity

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "dist", "build", "target"}


class InfrastructureAnalyzer:
    dimension = "infrastructure"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_helm = any(repo_path.rglob("Chart.yaml"))
        has_kustomize = any(repo_path.rglob("kustomization.yaml")) or any(repo_path.rglob("kustomization.yml"))
        has_terraform = any(repo_path.rglob("*.tf"))
        has_k8s_manifests = False
        has_resource_limits = False
        has_namespace = False
        has_quota = False

        for yaml_file in list(repo_path.rglob("*.yaml")) + list(repo_path.rglob("*.yml")):
            if any(d in yaml_file.relative_to(repo_path).parts for d in IGNORED_DIRS):
                continue
            try:
                content = yaml_file.read_text(errors="ignore")
            except OSError:
                continue
            if "apiVersion:" in content and "kind:" in content:
                has_k8s_manifests = True
            if "resources:" in content and ("limits:" in content or "requests:" in content):
                has_resource_limits = True
            if "kind: Namespace" in content:
                has_namespace = True
            if "kind: ResourceQuota" in content or "kind: LimitRange" in content:
                has_quota = True

        if not has_helm and not has_kustomize and not has_terraform:
            findings.append(Finding(
                category="iac",
                severity=Severity.high,
                description="No IaC tooling detected (no Helm chart, Kustomize, or Terraform)",
                recommendation="Generate Helm chart with values.yaml and environment overlays",
            ))
        if not has_k8s_manifests and not has_helm:
            findings.append(Finding(
                category="manifests",
                severity=Severity.high,
                description="No Kubernetes manifests found",
                recommendation="Create deployment, service, and ingress manifests",
            ))
        if has_k8s_manifests and not has_resource_limits:
            findings.append(Finding(
                category="resources",
                severity=Severity.medium,
                description="No resource limits/requests defined in manifests",
                recommendation="Add CPU and memory requests/limits to all containers",
            ))
        if has_k8s_manifests and not has_quota:
            findings.append(Finding(
                category="quota",
                severity=Severity.low,
                description="No ResourceQuota or LimitRange defined",
                recommendation="Add ResourceQuota and LimitRange for namespace governance",
            ))

        score = 100
        for f in findings:
            if f.severity == Severity.high:
                score -= 25
            elif f.severity == Severity.medium:
                score -= 12
            elif f.severity == Severity.low:
                score -= 5
        return DimensionScore(dimension="infrastructure", score=max(0, score), max_score=100, findings=findings)
