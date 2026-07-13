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
            Finding(category="gitignore", severity=Severity.low,
                    description="Missing .gitignore", recommendation="Add .gitignore"),
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

    def test_generates_dockerfile_fix(self, tmp_path: Path) -> None:
        """'dockerfile'/'container' findings must reach _fix_dockerfile() —
        previously filtered out by _SUPPORTED_CATEGORIES before ever
        reaching the (already-written) handler."""
        report = _make_report(findings=[
            Finding(category="dockerfile", severity=Severity.high,
                    description="No Dockerfile found", recommendation="Add one"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "Dockerfile"

    def test_generates_container_fix(self, tmp_path: Path) -> None:
        report = _make_report(findings=[
            Finding(category="container", severity=Severity.high,
                    description="Running as root", recommendation="Add USER"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "Dockerfile"

    def test_generates_health_endpoint_fix(self, tmp_path: Path) -> None:
        """'health' findings must reach _add_health_endpoint() — previously
        filtered out by _SUPPORTED_CATEGORIES before ever reaching the
        (already-written) handler."""
        report = _make_report(lang="python", framework="flask", findings=[
            Finding(category="health", severity=Severity.medium,
                    description="No health endpoint", recommendation="Add one"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "healthz.py"

    def test_generates_secrets_fix(self, tmp_path: Path) -> None:
        """'secrets' findings pass the _SUPPORTED_CATEGORIES filter and must
        now have a real handler instead of silently producing nothing."""
        report = _make_report(findings=[
            Finding(category="secrets", severity=Severity.critical,
                    description="Hardcoded password", recommendation="Externalize it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == ".env.example"

    def test_generates_logging_fix(self, tmp_path: Path) -> None:
        """'logging'/'structured' findings pass the _SUPPORTED_CATEGORIES
        filter and must now have a real handler instead of silently
        producing nothing."""
        report = _make_report(lang="python", findings=[
            Finding(category="logging", severity=Severity.medium,
                    description="No structured logging", recommendation="Add it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "logging_setup.py"

    def test_generates_structured_logging_fix(self, tmp_path: Path) -> None:
        report = _make_report(lang="javascript", findings=[
            Finding(category="structured", severity=Severity.medium,
                    description="No structured logging", recommendation="Add it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == "logger.js"


class TestLLMChanges:
    def test_llm_generates_change(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = '{"file_path": ".gitignore", "action": "create", "content": ".env\\n__pycache__", "explanation": "Add gitignore"}'

        report = _make_report(findings=[
            Finding(category="gitignore", severity=Severity.low,
                    description="Missing .gitignore", recommendation="Add it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 1
        assert result.changes[0].file_path == ".gitignore"

    def test_llm_bad_json_skips(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = "not valid json"

        report = _make_report(findings=[
            Finding(category="secrets", severity=Severity.high,
                    description="Hardcoded secret", recommendation="Fix"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 0

    def test_llm_none_skips(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = None

        report = _make_report(findings=[
            Finding(category="secrets", severity=Severity.high,
                    description="Hardcoded secret", recommendation="Fix"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 0


class TestSupportedCategoriesConsistency:
    def test_every_supported_category_has_a_handler(self, tmp_path: Path) -> None:
        """Every keyword in _SUPPORTED_CATEGORIES must actually be handled by
        _generate_change_deterministic(), or matching findings pass the
        filter and silently produce nothing."""
        from agentit.agents.codechange import _SUPPORTED_CATEGORIES

        for category in _SUPPORTED_CATEGORIES:
            report = _make_report(lang="python", framework="flask", findings=[
                Finding(category=category, severity=Severity.medium,
                        description=f"{category} finding", recommendation="fix it"),
            ])
            agent = CodeChangeAgent(report, tmp_path / f"out-{category}")
            result = agent.run()
            assert len(result.changes) == 1, f"category '{category}' passed the filter but produced no change"


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
        report = _make_report()
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 2
        summary_files = [f for f in result.files if f.path == "code-changes-summary.md"]
        assert len(summary_files) == 1
        assert "Code Changes" in summary_files[0].content


class TestResultModel:
    def test_summary_count(self, tmp_path: Path) -> None:
        report = _make_report()
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert isinstance(result, CodeChangeResult)
        assert "2 code changes" in result.summary

    def test_output_dir_created(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        agent = CodeChangeAgent(_make_report(), out)
        agent.run()
        assert out.exists()
