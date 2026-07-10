from __future__ import annotations

from pathlib import Path

from agentit.models import DimensionScore, Finding, Severity

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "dist", "build", "target"}


class CICDAnalyzer:
    dimension = "cicd"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_ci = False
        has_container = False
        has_gitops = False
        has_tekton = False

        ci_paths = [
            ".github/workflows", ".gitlab-ci.yml", "Jenkinsfile",
            ".circleci/config.yml", ".travis.yml", "azure-pipelines.yml",
            ".tekton",
        ]
        for ci_path in ci_paths:
            p = repo_path / ci_path
            if p.exists():
                has_ci = True
                if ".tekton" in ci_path:
                    has_tekton = True
                break

        for name in ["Dockerfile", "Containerfile", "Dockerfile.prod"]:
            if (repo_path / name).exists():
                has_container = True
                break

        all_text = ""
        for fp in repo_path.rglob("*.yaml"):
            if any(d in fp.relative_to(repo_path).parts for d in IGNORED_DIRS):
                continue
            try:
                all_text += fp.read_text(errors="ignore") + "\n"
            except OSError:
                continue
        for fp in repo_path.rglob("*.yml"):
            if any(d in fp.relative_to(repo_path).parts for d in IGNORED_DIRS):
                continue
            try:
                all_text += fp.read_text(errors="ignore") + "\n"
            except OSError:
                continue

        if "argoproj.io" in all_text or "kind: Application" in all_text:
            has_gitops = True

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

        score = 100
        for f in findings:
            if f.severity == Severity.high:
                score -= 25
            elif f.severity == Severity.medium:
                score -= 15
            elif f.severity == Severity.low:
                score -= 5
        return DimensionScore(dimension="cicd", score=max(0, score), max_score=100, findings=findings)
