from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from agentit.agents.dependency import DependencyAgent, DependencyResult
from agentit.models import (
    AssessmentReport,
    ArchitectureInfo,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)


def _make_report(
    *,
    repo_name: str = "test-app",
    languages: list[Language] | None = None,
    package_managers: list[str] | None = None,
    external_dependencies: list[str] | None = None,
    scores: list[DimensionScore] | None = None,
) -> AssessmentReport:
    if languages is None:
        languages = [Language(name="python", file_count=10, percentage=100.0)]
    return AssessmentReport(
        repo_url="https://github.com/org/test-app",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=languages,
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=package_managers or [],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=external_dependencies or [],
        ),
        scores=scores or [],
        criticality="medium",
        summary="test summary",
        remediation_plan=[],
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


class TestDependencyReport:
    def test_generates_dependency_report(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            package_managers=["pip"],
            external_dependencies=["requests", "log4j-core"],
            scores=[
                _score_with_finding("security", "vulnerability", "CVE-2023-32681 in requests"),
                _score_with_finding("security", "dependency", "Outdated packages detected"),
            ],
        )
        result = DependencyAgent(report, tmp_path / "out").run()

        md_files = [f for f in result.files if f.path == "dependency-report.md"]
        assert len(md_files) == 1

        content = md_files[0].content
        assert "# Dependency Report: test-app" in content
        assert "pip" in content
        assert "python" in content.lower()
        assert "Risk Indicators" in content
        assert "CRITICAL" in content  # vulnerability finding
        assert (tmp_path / "out" / "dependency-report.md").exists()


class TestRenovateConfig:
    def test_generates_renovate_config_python(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            package_managers=["pip"],
        )
        result = DependencyAgent(report, tmp_path / "out").run()

        renovate_files = [f for f in result.files if f.path == "renovate.json"]
        assert len(renovate_files) == 1

        config = json.loads(renovate_files[0].content)
        assert "config:recommended" in config["extends"]
        assert config["vulnerabilityAlerts"]["enabled"] is True
        assert "pip_requirements" in config["enabledManagers"]

        # Verify auto-merge for patches
        patch_rule = next(
            r for r in config["packageRules"] if r.get("automerge") is True
        )
        assert "patch" in patch_rule["matchUpdateTypes"]

        # Verify minor grouping
        minor_rule = next(
            r for r in config["packageRules"] if r.get("groupName") == "minor-updates"
        )
        assert "minor" in minor_rule["matchUpdateTypes"]

        assert (tmp_path / "out" / "renovate.json").exists()


class TestDependabotConfig:
    def test_generates_dependabot_config_node(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="javascript", file_count=20, percentage=100.0)],
            package_managers=["npm"],
        )
        result = DependencyAgent(report, tmp_path / "out").run()

        db_files = [f for f in result.files if f.path == ".github/dependabot.yml"]
        assert len(db_files) == 1

        config = yaml.safe_load(db_files[0].content)
        assert config["version"] == 2
        ecosystems = [u["package-ecosystem"] for u in config["updates"]]
        assert "npm" in ecosystems
        assert config["updates"][0]["schedule"]["interval"] == "weekly"
        assert (tmp_path / "out" / ".github" / "dependabot.yml").exists()

    def test_generates_dependabot_config_go(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="Go", file_count=15, percentage=100.0)],
            package_managers=["gomod"],
        )
        result = DependencyAgent(report, tmp_path / "out").run()

        db_files = [f for f in result.files if f.path == ".github/dependabot.yml"]
        assert len(db_files) == 1

        config = yaml.safe_load(db_files[0].content)
        ecosystems = [u["package-ecosystem"] for u in config["updates"]]
        assert "gomod" in ecosystems
        assert (tmp_path / "out" / ".github" / "dependabot.yml").exists()


class TestNoEcosystems:
    def test_no_configs_without_ecosystems(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="unknown-lang", file_count=1, percentage=100.0)],
            package_managers=[],
        )
        result = DependencyAgent(report, tmp_path / "out").run()

        # Should still generate the report
        assert any(f.path == "dependency-report.md" for f in result.files)
        # But no renovate or dependabot configs
        assert not any(f.path == "renovate.json" for f in result.files)
        assert not any(f.path == ".github/dependabot.yml" for f in result.files)
