from __future__ import annotations

from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity


class CICDAnalyzer:
    dimension = "cicd"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_ci = False
        has_tekton = False

        ci_paths = [
            ".github/workflows", ".gitlab-ci.yml", "Jenkinsfile",
            ".circleci/config.yml", ".travis.yml", "azure-pipelines.yml",
            ".tekton",
        ]
        for ci_path in ci_paths:
            if (repo_path / ci_path).exists():
                has_ci = True
                if ".tekton" in ci_path:
                    has_tekton = True
                break

        has_container = any(
            (repo_path / name).exists()
            for name in ["Dockerfile", "Containerfile", "Dockerfile.prod"]
        )

        has_argoproj = False
        has_app_kind = False
        for _, content in iter_yaml_files(repo_path):
            if "argoproj.io" in content:
                has_argoproj = True
            if "kind: Application" in content:
                has_app_kind = True
            if "kind: Pipeline" in content and "tekton.dev" in content:
                has_tekton = True
                has_ci = True
        has_gitops = has_argoproj and has_app_kind

        if not has_ci:
            findings.append(Finding(
                category="pipeline",
                severity=Severity.high,
                description="No CI/CD pipeline configuration found",
                recommendation="Create Tekton Pipeline for build/test/scan/deploy",
            ))
        if not has_container:
            findings.append(Finding(
                category="container",
                severity=Severity.high,
                description="No Containerfile or Dockerfile found",
                recommendation="Create multi-stage Containerfile with UBI base image",
            ))
        if not has_gitops:
            findings.append(Finding(
                category="gitops",
                severity=Severity.medium,
                description="No GitOps configuration (Argo CD) detected",
                recommendation="Create Argo CD Application for GitOps delivery",
            ))
        if has_ci and not has_tekton:
            findings.append(Finding(
                category="pipeline",
                severity=Severity.low,
                description="CI pipeline exists but is not Tekton-based",
                recommendation="Consider migrating to OpenShift Pipelines (Tekton) for OpenShift-native CI",
            ))

        return DimensionScore(
            dimension="cicd",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
