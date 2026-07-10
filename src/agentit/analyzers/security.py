from __future__ import annotations

import re
from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_text_files, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("password", re.compile(r"""(?:password|passwd|pwd)\s*[:=]\s*['"]?[^\s'"]{8,}""", re.IGNORECASE)),
    ("api_key", re.compile(r"""(?:api[_-]?key|apikey)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{16,}""", re.IGNORECASE)),
    ("secret_key", re.compile(r"""(?:secret[_-]?key|secret)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{16,}""", re.IGNORECASE)),
    ("token", re.compile(r"""(?:auth[_-]?token|access[_-]?token|bearer)\s*[:=]\s*['"]?[a-zA-Z0-9_\-]{16,}""", re.IGNORECASE)),
    ("sk_prefix", re.compile(r"""['"]sk-[a-zA-Z0-9]{10,}['"]""")),
    ("aws_key", re.compile(r"""AKIA[0-9A-Z]{16}""")),
    ("private_key", re.compile(r"""-----BEGIN (?:RSA |EC )?PRIVATE KEY-----""")),
]


class SecurityAnalyzer:
    dimension = "security"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        findings.extend(self._check_secrets(repo_path))
        findings.extend(self._check_dockerfile(repo_path))
        findings.extend(self._check_network_policies(repo_path))
        findings.extend(self._check_scanning(repo_path))
        findings.extend(self._check_base_image(repo_path))

        return DimensionScore(
            dimension="security",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )

    def _check_secrets(self, repo_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for file_path, content in iter_text_files(repo_path):
            for secret_type, pattern in SECRET_PATTERNS:
                if pattern.search(content):
                    rel_path = str(file_path.relative_to(repo_path))
                    findings.append(Finding(
                        category="secrets",
                        severity=Severity.critical,
                        description=f"Potential {secret_type} found in {rel_path}",
                        file_path=rel_path,
                        recommendation="Migrate to ExternalSecrets Operator or HashiCorp Vault",
                    ))
        return findings

    def _check_dockerfile(self, repo_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        dockerfiles = list(repo_path.glob("Dockerfile*")) + list(repo_path.glob("Containerfile*"))

        if not dockerfiles:
            findings.append(Finding(
                category="container",
                severity=Severity.medium,
                description="No Dockerfile or Containerfile found",
                recommendation="Create a multi-stage Containerfile using UBI base image",
            ))
            return findings

        for df in dockerfiles:
            try:
                content = df.read_text(errors="ignore")
            except OSError:
                continue
            rel_path = str(df.relative_to(repo_path))

            if not re.search(r"^\s*USER\s+", content, re.MULTILINE):
                findings.append(Finding(
                    category="container",
                    severity=Severity.high,
                    description=f"Container runs as root (no USER directive) in {rel_path}",
                    file_path=rel_path,
                    recommendation="Add USER 1001 directive to run as non-root",
                ))

            if not re.search(r"^\s*HEALTHCHECK\s+", content, re.MULTILINE):
                findings.append(Finding(
                    category="container",
                    severity=Severity.medium,
                    description=f"No HEALTHCHECK defined in {rel_path}",
                    file_path=rel_path,
                    recommendation="Add HEALTHCHECK for container orchestration readiness probes",
                ))

            if re.search(r":latest\b", content):
                findings.append(Finding(
                    category="container",
                    severity=Severity.medium,
                    description=f"Using :latest tag in base image in {rel_path}",
                    file_path=rel_path,
                    recommendation="Pin base image to specific version for reproducible builds",
                ))

        return findings

    def _check_network_policies(self, repo_path: Path) -> list[Finding]:
        seen_yaml = False
        for _, content in iter_yaml_files(repo_path):
            seen_yaml = True
            if "NetworkPolicy" in content:
                return []

        if not seen_yaml:
            return []

        return [Finding(
            category="network",
            severity=Severity.high,
            description="No NetworkPolicy manifests found",
            recommendation="Add deny-all default NetworkPolicy with explicit allow rules",
        )]

    def _check_scanning(self, repo_path: Path) -> list[Finding]:
        scan_indicators = ["trivy", "grype", "snyk", "stackrox", "acs", "clair", "anchore"]
        for ci_file in list(repo_path.rglob(".github/workflows/*.yml")) + list(repo_path.rglob(".gitlab-ci.yml")) + list(repo_path.rglob("Jenkinsfile")):
            try:
                content = ci_file.read_text(errors="ignore").lower()
                if any(s in content for s in scan_indicators):
                    return []
            except OSError:
                continue
        return [Finding(
            category="scanning",
            severity=Severity.high,
            description="No container or dependency vulnerability scanning detected in CI",
            recommendation="Add ACS (StackRox) or Trivy scanning to the CI pipeline",
        )]

    def _check_base_image(self, repo_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for df in list(repo_path.glob("Dockerfile*")) + list(repo_path.glob("Containerfile*")):
            try:
                content = df.read_text(errors="ignore")
            except OSError:
                continue
            if re.search(r"^\s*FROM\s+", content, re.MULTILINE) and not re.search(r"FROM\s+.*(?:ubi|redhat|registry\.access\.redhat)", content, re.IGNORECASE):
                findings.append(Finding(
                    category="container",
                    severity=Severity.low,
                    description=f"Base image is not UBI (Red Hat Universal Base Image) in {df.name}",
                    file_path=str(df.relative_to(repo_path)),
                    recommendation="Use registry.access.redhat.com/ubi9/ubi-minimal for supported, secure base image",
                ))
        return findings
