"""Tests for the Code Change Agent."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentit.agents.codechange import CodeChangeAgent, CodeChangeResult
from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Framework,
    Language,
    Severity,
    StackInfo,
)


def _make_report(
    lang: str = "python",
    framework: str | None = None,
    findings: list[Finding] | None = None,
    criticality: str = "high",
) -> AssessmentReport:
    frameworks = [Framework(name=framework, language=lang)] if framework else []
    if findings is None:
        findings = [
            Finding(category="container security", severity=Severity.high,
                    description="No Containerfile", recommendation="Add Dockerfile"),
            Finding(category="health endpoint", severity=Severity.medium,
                    description="No health check", recommendation="Add /healthz"),
            Finding(category="opentelemetry", severity=Severity.medium,
                    description="No tracing", recommendation="Add OTel"),
        ]
    return AssessmentReport(
        repo_url="https://github.com/org/test-app",
        repo_name="test-app",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=[Language(name=lang, file_count=10, percentage=100.0)],
            frameworks=frameworks, databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[],
        ),
        scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=findings)],
        criticality=criticality,
        summary="test",
        remediation_plan=[],
    )


class TestDeterministicChanges:
    def test_generates_dockerfile_fix(self, tmp_path: Path) -> None:
        report = _make_report(findings=[
            Finding(category="dockerfile", severity=Severity.high,
                    description="Running as root", recommendation="Add USER"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "Dockerfile"
        assert "USER 1001" in result.changes[0].content
        assert "HEALTHCHECK" in result.changes[0].content

    def test_dockerfile_uses_ubi_base(self, tmp_path: Path) -> None:
        for lang in ("python", "go", "node", "java"):
            report = _make_report(lang=lang, findings=[
                Finding(category="container", severity=Severity.high,
                        description="No Dockerfile", recommendation="Add it"),
            ])
            agent = CodeChangeAgent(report, tmp_path / f"out-{lang}")
            result = agent.run()
            assert "ubi9" in result.changes[0].content, f"No UBI base for {lang}"

    def test_generates_health_endpoint_python(self, tmp_path: Path) -> None:
        report = _make_report(lang="python", framework="Flask", findings=[
            Finding(category="health", severity=Severity.medium,
                    description="No health check", recommendation="Add /healthz"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert "healthz" in result.changes[0].content

    def test_generates_health_endpoint_node(self, tmp_path: Path) -> None:
        report = _make_report(lang="javascript", framework="Express", findings=[
            Finding(category="healthcheck", severity=Severity.medium,
                    description="No health check", recommendation="Add /healthz"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert "healthz" in result.changes[0].content

    def test_generates_health_endpoint_go(self, tmp_path: Path) -> None:
        report = _make_report(lang="go", framework="Gin", findings=[
            Finding(category="health", severity=Severity.medium,
                    description="No health check", recommendation="Add /healthz"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert "healthz" in result.changes[0].content

    def test_generates_gitignore(self, tmp_path: Path) -> None:
        report = _make_report(findings=[
            Finding(category="gitignore", severity=Severity.low,
                    description="Missing .gitignore", recommendation="Add it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert result.changes[0].file_path == ".gitignore"
        assert ".env" in result.changes[0].content

    def test_generates_otel_python(self, tmp_path: Path) -> None:
        report = _make_report(lang="python", findings=[
            Finding(category="opentelemetry", severity=Severity.medium,
                    description="No tracing", recommendation="Add OTel"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert "opentelemetry" in result.changes[0].content.lower()

    def test_generates_otel_node(self, tmp_path: Path) -> None:
        report = _make_report(lang="javascript", findings=[
            Finding(category="otel", severity=Severity.medium,
                    description="No tracing", recommendation="Add OTel"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert "opentelemetry" in result.changes[0].content.lower()


class TestLLMChanges:
    def test_llm_generates_change(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = '{"file_path": "Dockerfile", "action": "modify", "content": "FROM ubi9", "explanation": "Fix base image"}'

        report = _make_report(findings=[
            Finding(category="container", severity=Severity.high,
                    description="Bad base image", recommendation="Use UBI"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "Dockerfile"
        assert result.changes[0].content == "FROM ubi9"

    def test_llm_bad_json_skips(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = "not valid json"

        report = _make_report(findings=[
            Finding(category="container", severity=Severity.high,
                    description="Issue", recommendation="Fix"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 0

    def test_llm_none_skips(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = None

        report = _make_report(findings=[
            Finding(category="container", severity=Severity.high,
                    description="Issue", recommendation="Fix"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 0


class TestNoActionableFindings:
    def test_no_changes_for_irrelevant_findings(self, tmp_path: Path) -> None:
        report = _make_report(findings=[
            Finding(category="network", severity=Severity.high,
                    description="No NetworkPolicy", recommendation="Add it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 0

    def test_empty_findings(self, tmp_path: Path) -> None:
        report = _make_report(findings=[])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 0


class TestMultipleChanges:
    def test_generates_multiple_changes(self, tmp_path: Path) -> None:
        report = _make_report(framework="Flask")  # framework needed for health endpoint
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 3
        summary_files = [f for f in result.files if f.path == "code-changes-summary.md"]
        assert len(summary_files) == 1
        assert "Code Changes" in summary_files[0].content


class TestResultModel:
    def test_summary_count(self, tmp_path: Path) -> None:
        report = _make_report(framework="Flask")
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert isinstance(result, CodeChangeResult)
        assert "3 code changes" in result.summary

    def test_output_dir_created(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        agent = CodeChangeAgent(_make_report(), out)
        agent.run()
        assert out.exists()
