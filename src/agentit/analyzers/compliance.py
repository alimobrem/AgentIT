from __future__ import annotations

from pathlib import Path

from agentit.analyzers.base import calculate_score, is_ignored, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity
from agentit.remediation.audit_wire import has_audit_usage

_AUDIT_MODULE_NAMES = frozenset({
    "audit.py", "audit.go", "audit.js", "audit.ts", "audit_log.py",
})


class ComplianceAnalyzer:
    dimension = "compliance"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        has_license = (repo_path / "LICENSE").exists() or (repo_path / "LICENSE.md").exists() or (repo_path / "LICENCE").exists()
        has_sbom = False
        has_audit_module = False
        has_audit_callsite = False
        has_policy = False

        for fp in repo_path.rglob("*"):
            if not fp.is_file() or is_ignored(fp, repo_path):
                continue
            name = fp.name.lower()
            if "sbom" in name or "bom" in name:
                has_sbom = True
            if name in _AUDIT_MODULE_NAMES:
                # Module alone is insufficient — import/call must appear elsewhere.
                # Root-only orphans (pinky#8 theater) do not count; require a
                # package path (e.g. src/agentit/audit.py, apps/api/.../audit.py).
                try:
                    rel = fp.relative_to(repo_path).as_posix()
                except ValueError:
                    continue
                if "/" in rel:
                    has_audit_module = True
                continue

            if fp.suffix.lower() in {".py", ".ts", ".js", ".go"}:
                try:
                    content = fp.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if has_audit_usage(content):
                    has_audit_callsite = True

        # App audit logging ≠ apiserver Policy / advisory ConfigMap text.
        # Do not clear on YAML "audit"+"log" substrings (that false-cleared
        # via audit-policy ConfigMaps without any app wiring).
        for _, content in iter_yaml_files(repo_path):
            if "kind: Policy" in content or "kind: ClusterPolicy" in content or "kind: ConstraintTemplate" in content:
                has_policy = True

        has_audit_log = has_audit_module and has_audit_callsite

        if not has_license:
            findings.append(Finding(
                category="license",
                severity=Severity.high,
                description="No LICENSE file found",
                recommendation="Add a LICENSE file (Apache 2.0 recommended for enterprise open source)",
                source="analyzer:compliance",
            ))
        if not has_sbom:
            findings.append(Finding(
                category="sbom",
                severity=Severity.high,
                description="No SBOM (Software Bill of Materials) found",
                recommendation="Generate SBOM using Syft, store in ODF",
                source="analyzer:compliance",
            ))
        if not has_audit_log:
            findings.append(Finding(
                category="audit",
                severity=Severity.high,
                description="No audit logging implementation detected",
                recommendation=(
                    "Add an audit logging module in the app package and wire it "
                    "into privileged/mutating request handlers (import + call site)"
                ),
                source="analyzer:compliance",
            ))
        if not has_policy:
            findings.append(Finding(
                category="policy",
                severity=Severity.medium,
                description="No admission policies (Kyverno/OPA/Gatekeeper) found",
                recommendation="Create Kyverno policies for resource limits, labels, approved base images",
                source="analyzer:compliance",
            ))

        return DimensionScore(
            dimension="compliance",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
