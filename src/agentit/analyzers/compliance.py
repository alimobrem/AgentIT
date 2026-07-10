from __future__ import annotations

from pathlib import Path

from agentit.analyzers.base import calculate_score, is_ignored, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity


class ComplianceAnalyzer:
    dimension = "compliance"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_license = (repo_path / "LICENSE").exists() or (repo_path / "LICENSE.md").exists() or (repo_path / "LICENCE").exists()
        has_sbom = False
        has_audit_log = False
        has_policy = False

        for fp in repo_path.rglob("*"):
            if not fp.is_file() or is_ignored(fp, repo_path):
                continue
            name = fp.name.lower()
            if "sbom" in name or "bom" in name:
                has_sbom = True
            if name in ("audit.py", "audit.go", "audit.js", "audit.ts", "audit_log.py"):
                has_audit_log = True

        for _, content in iter_yaml_files(repo_path):
            if "kind: Policy" in content or "kind: ClusterPolicy" in content or "kind: ConstraintTemplate" in content:
                has_policy = True
            if "audit" in content.lower() and "log" in content.lower():
                has_audit_log = True

        if not has_license:
            findings.append(Finding(
                category="license",
                severity=Severity.high,
                description="No LICENSE file found",
                recommendation="Add a LICENSE file (Apache 2.0 recommended for enterprise open source)",
            ))
        if not has_sbom:
            findings.append(Finding(
                category="sbom",
                severity=Severity.high,
                description="No SBOM (Software Bill of Materials) found",
                recommendation="Generate SBOM using Syft, store in ODF",
            ))
        if not has_audit_log:
            findings.append(Finding(
                category="audit",
                severity=Severity.high,
                description="No audit logging implementation detected",
                recommendation="Add audit logging for privileged actions and data access",
            ))
        if not has_policy:
            findings.append(Finding(
                category="policy",
                severity=Severity.medium,
                description="No admission policies (Kyverno/OPA/Gatekeeper) found",
                recommendation="Create Kyverno policies for resource limits, labels, approved base images",
            ))

        return DimensionScore(
            dimension="compliance",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
