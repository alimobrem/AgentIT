from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from conftest import make_report

from agentit.agents.compliance import ComplianceAgent, ComplianceResult
from agentit.models import (
    DimensionScore,
    Finding,
    Severity,
)


def _score_with_finding(dimension: str, category: str, desc: str) -> DimensionScore:
    return DimensionScore(
        dimension=dimension,
        score=30,
        max_score=100,
        findings=[
            Finding(
                category=category,
                severity=Severity.high,
                description=desc,
                recommendation="fix it",
            ),
        ],
    )


class TestKyvernoPolicies:
    def test_generates_kyverno_policies(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("compliance", "policy", "No admission policies")],
        )
        result = ComplianceAgent(report, tmp_path / "out").run()

        kp = [f for f in result.files if f.path == "kyverno-policies.yaml"]
        assert len(kp) == 1

        docs = list(yaml.safe_load_all(kp[0].content))
        assert len(docs) == 4

        names = [d["metadata"]["name"] for d in docs]
        assert "require-labels" in names
        assert "require-resource-limits" in names
        assert "restrict-image-registries" in names
        assert "disallow-latest-tag" in names

        for doc in docs:
            assert doc["kind"] == "Policy"
            assert doc["apiVersion"] == "kyverno.io/v1"

        assert (tmp_path / "out" / "kyverno-policies.yaml").exists()

    def test_skips_kyverno_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ComplianceAgent(report, tmp_path / "out").run()
        assert not any(f.path == "kyverno-policies.yaml" for f in result.files)


class TestSbomTask:
    def test_generates_sbom_task(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("compliance", "sbom", "No SBOM found")],
        )
        result = ComplianceAgent(report, tmp_path / "out").run()

        sbom = [f for f in result.files if f.path == "sbom-generate-task.yaml"]
        assert len(sbom) == 1
        assert "kind: Task" in sbom[0].content
        assert "syft" in sbom[0].content
        assert "cyclonedx-json" in sbom[0].content
        assert (tmp_path / "out" / "sbom-generate-task.yaml").exists()

    def test_skips_sbom_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ComplianceAgent(report, tmp_path / "out").run()
        assert not any(f.path == "sbom-generate-task.yaml" for f in result.files)


class TestComplianceEvidence:
    def test_generates_compliance_evidence(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("security", "network", "Open ports")],
        )
        result = ComplianceAgent(report, tmp_path / "out").run()

        ce = [f for f in result.files if f.path == "compliance-evidence.md"]
        assert len(ce) == 1
        assert "# Compliance Evidence Report" in ce[0].content
        assert "Security Controls" in ce[0].content
        assert "Access Controls" in ce[0].content
        assert "Audit Logging" in ce[0].content
        assert "Data Protection" in ce[0].content
        assert (tmp_path / "out" / "compliance-evidence.md").exists()

    def test_evidence_always_generated(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ComplianceAgent(report, tmp_path / "out").run()
        assert any(f.path == "compliance-evidence.md" for f in result.files)


class TestAuditPolicy:
    def test_generates_audit_policy(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("compliance", "audit", "No audit logging configured")],
        )
        result = ComplianceAgent(report, tmp_path / "out").run()

        ap = [f for f in result.files if f.path == "audit-policy.yaml"]
        assert len(ap) == 1

        doc = yaml.safe_load(ap[0].content)
        assert doc["kind"] == "Policy"
        assert doc["apiVersion"] == "audit.k8s.io/v1"
        assert any(
            "create" in rule.get("verbs", [])
            for rule in doc["rules"]
        )
        assert (tmp_path / "out" / "audit-policy.yaml").exists()

    def test_skips_audit_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ComplianceAgent(report, tmp_path / "out").run()
        assert not any(f.path == "audit-policy.yaml" for f in result.files)


class TestComplianceResult:
    def test_summary_message(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[
                _score_with_finding("compliance", "policy", "No policies"),
                _score_with_finding("compliance", "sbom", "No SBOM"),
                _score_with_finding("compliance", "audit", "No audit"),
            ],
        )
        result = ComplianceAgent(report, tmp_path / "out").run()
        assert "Generated" in result.summary
        assert str(len(result.files)) in result.summary


class TestComplianceCronJob:
    def test_generates_compliance_cronjob(self, tmp_path: Path) -> None:
        report = make_report()
        result = ComplianceAgent(report, tmp_path / "out").run()
        cw = [f for f in result.files if f.path == "compliance-cronjob.yaml"]
        assert len(cw) == 1

        doc = yaml.safe_load(cw[0].content)
        assert doc["kind"] == "CronJob"
        assert doc["apiVersion"] == "batch/v1"
        assert doc["spec"]["schedule"] == "0 3 1 * *"
        assert doc["spec"]["concurrencyPolicy"] == "Forbid"
