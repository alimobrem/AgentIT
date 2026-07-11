"""Tests for the Release Coordinator Agent."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from agentit.agents.release import ReleaseCoordinatorAgent, ReleaseResult
from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)


def _make_report(criticality: str = "medium") -> AssessmentReport:
    return AssessmentReport(
        repo_url="https://github.com/org/test-app",
        repo_name="test-app",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
        ),
        scores=[
            DimensionScore(
                dimension="security", score=50, max_score=100,
                findings=[
                    Finding(category="network", severity=Severity.high,
                            description="No NetworkPolicy", recommendation="Add it"),
                ],
            ),
        ],
        criticality=criticality,
        summary="test",
        remediation_plan=[],
    )


class TestAnalysisTemplate:
    def test_generates_analysis_template(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        at_files = [f for f in result.files if f.path == "analysis-template.yaml"]
        assert len(at_files) == 1

        doc = yaml.safe_load(at_files[0].content)
        assert doc["kind"] == "AnalysisTemplate"
        assert doc["apiVersion"] == "argoproj.io/v1alpha1"
        assert len(doc["spec"]["metrics"]) == 2

        success_metric = doc["spec"]["metrics"][0]
        assert success_metric["name"] == "success-rate"
        assert success_metric["failureLimit"] == 2
        assert "prometheus" in success_metric["provider"]
        assert "0.95" in success_metric["successCondition"]

    def test_analysis_template_uses_app_name(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        at_file = [f for f in result.files if f.path == "analysis-template.yaml"][0]
        doc = yaml.safe_load(at_file.content)
        assert doc["metadata"]["name"] == "test-app-success-rate"
        assert "test-app" in doc["spec"]["metrics"][0]["provider"]["prometheus"]["query"]


class TestRolloutPatch:
    def test_generates_rollout_with_analysis(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        rollout_files = [f for f in result.files if f.path == "rollout-patch.yaml"]
        assert len(rollout_files) == 1

        doc = yaml.safe_load(rollout_files[0].content)
        assert doc["kind"] == "Rollout"
        steps = doc["spec"]["strategy"]["canary"]["steps"]
        analysis_steps = [s for s in steps if "analysis" in s]
        assert len(analysis_steps) >= 3

    def test_auto_promotion_disabled_for_critical(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(criticality="critical"), tmp_path / "out")
        result = agent.run()
        rollout = [f for f in result.files if f.path == "rollout-patch.yaml"][0]
        doc = yaml.safe_load(rollout.content)
        assert doc["spec"]["strategy"]["canary"]["autoPromotionEnabled"] is False

    def test_auto_promotion_disabled_for_high(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(criticality="high"), tmp_path / "out")
        result = agent.run()
        rollout = [f for f in result.files if f.path == "rollout-patch.yaml"][0]
        doc = yaml.safe_load(rollout.content)
        assert doc["spec"]["strategy"]["canary"]["autoPromotionEnabled"] is False

    def test_auto_promotion_enabled_for_low(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(criticality="low"), tmp_path / "out")
        result = agent.run()
        rollout = [f for f in result.files if f.path == "rollout-patch.yaml"][0]
        doc = yaml.safe_load(rollout.content)
        assert doc["spec"]["strategy"]["canary"]["autoPromotionEnabled"] is True

    def test_auto_promotion_enabled_for_medium(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(criticality="medium"), tmp_path / "out")
        result = agent.run()
        rollout = [f for f in result.files if f.path == "rollout-patch.yaml"][0]
        doc = yaml.safe_load(rollout.content)
        assert doc["spec"]["strategy"]["canary"]["autoPromotionEnabled"] is True

    def test_rollback_window(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        rollout = [f for f in result.files if f.path == "rollout-patch.yaml"][0]
        doc = yaml.safe_load(rollout.content)
        assert doc["spec"]["rollbackWindow"]["revisions"] == 2


class TestRollbackPolicy:
    def test_generates_rollback_policy(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        policy = [f for f in result.files if f.path == "rollback-policy.yaml"]
        assert len(policy) == 1

        doc = yaml.safe_load(policy[0].content)
        assert doc["kind"] == "ConfigMap"
        assert "abort-command" in doc["data"]
        assert "undo-command" in doc["data"]

    def test_error_budget_varies_by_criticality(self, tmp_path: Path) -> None:
        for crit, expected in [("critical", "0.01%"), ("high", "0.1%"), ("medium", "0.5%"), ("low", "1.0%")]:
            agent = ReleaseCoordinatorAgent(_make_report(criticality=crit), tmp_path / f"out-{crit}")
            result = agent.run()
            policy = [f for f in result.files if f.path == "rollback-policy.yaml"][0]
            doc = yaml.safe_load(policy.content)
            assert doc["data"]["error-budget-threshold"] == expected, f"Failed for {crit}"


class TestReleaseRunbook:
    def test_generates_runbook(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        runbooks = [f for f in result.files if f.path == "release-runbook.md"]
        assert len(runbooks) == 1
        assert "Pre-Deployment Checklist" in runbooks[0].content
        assert "Rollback Procedures" in runbooks[0].content
        assert "kubectl argo rollouts abort" in runbooks[0].content


class TestReleaseResult:
    def test_summary_count(self, tmp_path: Path) -> None:
        agent = ReleaseCoordinatorAgent(_make_report(), tmp_path / "out")
        result = agent.run()
        assert isinstance(result, ReleaseResult)
        assert "4 release coordination artifacts" in result.summary

    def test_output_dir_created(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        agent = ReleaseCoordinatorAgent(_make_report(), out)
        agent.run()
        assert out.exists()
        assert (out / "analysis-template.yaml").exists()
        assert (out / "rollout-patch.yaml").exists()
        assert (out / "rollback-policy.yaml").exists()
        assert (out / "release-runbook.md").exists()
