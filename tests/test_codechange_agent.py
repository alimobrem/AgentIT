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
            Finding(category="gitignore", severity=Severity.high,
                    description="Missing .gitignore", recommendation="Fix"),
        ])
        agent = CodeChangeAgent(report, tmp_path / "out", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 0

    def test_llm_none_skips(self, tmp_path: Path) -> None:
        llm = MagicMock()
        llm._chat.return_value = None

        report = _make_report(findings=[
            Finding(category="gitignore", severity=Severity.high,
                    description="Missing .gitignore", recommendation="Fix"),
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


class TestExcludedCategoriesNeverReachCodeChange:
    """dockerfile/container/secrets must never be handled by this agent --
    both its deterministic and LLM paths do a blind full-file overwrite
    with no knowledge of the existing file, bypassing every guardrail the
    skill-engine path has. Confirmed live-broken (PulseSRE/pulse-agent#2,
    PulseSRE/pulse-ui#39) -- see the module docstring. Regression coverage
    so a future change can't silently re-add these to _SUPPORTED_CATEGORIES
    without a human reconsidering the tradeoff explicitly."""

    @pytest.mark.parametrize("category", ["dockerfile", "container", "secrets"])
    def test_excluded_category_produces_no_change_deterministic(
        self, tmp_path: Path, category: str,
    ) -> None:
        report = _make_report(findings=[
            Finding(category=category, severity=Severity.high,
                    description=f"{category} finding", recommendation="fix it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / f"out-{category}")
        result = agent.run()
        assert len(result.changes) == 0

    @pytest.mark.parametrize("category", ["dockerfile", "container", "secrets"])
    def test_excluded_category_never_calls_llm(
        self, tmp_path: Path, category: str,
    ) -> None:
        """Even with an LLM client configured, excluded categories must be
        filtered out before _generate_change_with_llm() is ever called --
        never trust the LLM to correctly refuse on its own."""
        llm = MagicMock()
        llm._chat.return_value = '{"file_path": "Dockerfile", "action": "create", "content": "FROM x", "explanation": "x"}'

        report = _make_report(findings=[
            Finding(category=category, severity=Severity.high,
                    description=f"{category} finding", recommendation="fix it"),
        ])
        agent = CodeChangeAgent(report, tmp_path / f"out-llm-{category}", llm_client=llm)
        result = agent.run()
        assert len(result.changes) == 0
        llm._chat.assert_not_called()

    def test_excluded_categories_not_in_supported_categories(self) -> None:
        from agentit.agents.codechange import _SUPPORTED_CATEGORIES

        assert "dockerfile" not in _SUPPORTED_CATEGORIES
        assert "container" not in _SUPPORTED_CATEGORIES
        assert "secrets" not in _SUPPORTED_CATEGORIES


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
