from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agentit.agents.orchestrator import (
    AgentResult,
    FleetOrchestrator,
    OrchestrationPlan,
)
from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)


def _make_report(
    *,
    criticality: str = "medium",
    overall_score: float | None = None,
    scores: list[DimensionScore] | None = None,
) -> AssessmentReport:
    scores = scores or [
        DimensionScore(
            dimension="security",
            score=50,
            max_score=100,
            findings=[
                Finding(
                    category="network",
                    severity=Severity.high,
                    description="No NetworkPolicy",
                    recommendation="Add NetworkPolicy",
                ),
            ],
        ),
    ]
    report = AssessmentReport(
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
        scores=scores,
        criticality=criticality,
        summary="test summary",
        remediation_plan=[],
    )
    # Override overall_score after model_post_init if requested
    if overall_score is not None:
        report.overall_score = overall_score
    return report


class TestPlanSelectsAgents:
    def test_plan_selects_core_agents(self, tmp_path: Path) -> None:
        """Medium criticality -> 4 core agents + chaos."""
        report = _make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        for agent in ("security", "observability", "cicd", "compliance"):
            assert agent in plan.agents_to_run
        assert "chaos" in plan.agents_to_run

    def test_plan_adds_extra_agents_for_high_criticality(self, tmp_path: Path) -> None:
        """High criticality -> adds dependency, incident, cost."""
        report = _make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        for agent in ("dependency", "incident", "cost"):
            assert agent in plan.agents_to_run

    def test_plan_skips_chaos_for_critical(self, tmp_path: Path) -> None:
        """Critical criticality -> no chaos."""
        report = _make_report(criticality="critical")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        assert "chaos" not in plan.agents_to_run

    def test_plan_adds_retirement_for_low_score(self, tmp_path: Path) -> None:
        """Score < 30 -> retirement agent included."""
        report = _make_report(overall_score=20.0)
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        assert "retirement" in plan.agents_to_run


class TestAutoApprove:
    def test_auto_approve_for_low_crit_high_score(self, tmp_path: Path) -> None:
        """Low criticality, score 80, no criticals -> auto_approve=True."""
        scores = [
            DimensionScore(
                dimension="security",
                score=80,
                max_score=100,
                findings=[
                    Finding(
                        category="network",
                        severity=Severity.low,
                        description="Minor issue",
                        recommendation="Optional fix",
                    ),
                ],
            ),
        ]
        report = _make_report(criticality="low", scores=scores, overall_score=80.0)
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        assert plan.auto_approve is True

    def test_no_auto_approve_with_critical_findings(self, tmp_path: Path) -> None:
        """Has critical findings -> auto_approve=False."""
        scores = [
            DimensionScore(
                dimension="security",
                score=80,
                max_score=100,
                findings=[
                    Finding(
                        category="auth",
                        severity=Severity.critical,
                        description="No authentication",
                        recommendation="Add auth",
                    ),
                ],
            ),
        ]
        report = _make_report(criticality="low", scores=scores, overall_score=80.0)
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        assert plan.auto_approve is False


class TestRun:
    def test_run_executes_agents(self, tmp_path: Path) -> None:
        """Run with a real report -> results have success=True for available agents."""
        report = _make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = orch.run()

        successful = [r for r in result.agent_results if r.success]
        assert len(successful) >= 4  # At least the 4 core agents

        agent_names = {r.agent_name for r in successful}
        for core in ("security", "observability", "cicd", "compliance"):
            assert core in agent_names

    def test_run_writes_summary(self, tmp_path: Path) -> None:
        """Verify orchestration-summary.md exists after run."""
        report = _make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        orch.run()

        summary_path = tmp_path / "out" / "orchestration-summary.md"
        assert summary_path.exists()
        content = summary_path.read_text()
        assert "Orchestration Summary" in content
        assert "test-app" in content


class TestConflicts:
    def test_conflict_detection_security_blocker(self, tmp_path: Path) -> None:
        """Mock security agent failure -> blocker conflict."""
        report = _make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        # Simulate results with security failure
        results = [
            AgentResult(
                agent_name="security",
                category="security",
                files_generated=[],
                success=False,
                error="Agent crashed",
            ),
            AgentResult(
                agent_name="observability",
                category="observability",
                files_generated=["dashboards.json"],
                success=True,
            ),
        ]

        conflicts = orch._detect_conflicts(results)
        assert len(conflicts) == 1
        assert conflicts[0]["type"] == "blocker"
        assert conflicts[0]["winner"] == "security"


class TestRecommendation:
    def test_recommendation_blocked(self, tmp_path: Path) -> None:
        """Conflict present -> BLOCKED recommendation."""
        report = _make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        plan = OrchestrationPlan(
            repo_url="https://github.com/org/test-app",
            criticality="medium",
            score=50.0,
            agents_to_run=["security"],
            gates_required=["final-approval"],
            auto_approve=False,
        )
        results = [
            AgentResult(
                agent_name="security",
                category="security",
                files_generated=[],
                success=False,
                error="crashed",
            ),
        ]
        conflicts = [{"type": "blocker", "agents": ["security"], "resolution": "blocked", "winner": "security"}]

        rec = orch._generate_recommendation(plan, results, conflicts)
        assert rec.startswith("BLOCKED")

    def test_recommendation_auto_approved(self, tmp_path: Path) -> None:
        """auto_approve=True, all succeed -> AUTO-APPROVED recommendation."""
        report = _make_report(criticality="low", overall_score=80.0)
        orch = FleetOrchestrator(report, tmp_path / "out")

        plan = OrchestrationPlan(
            repo_url="https://github.com/org/test-app",
            criticality="low",
            score=80.0,
            agents_to_run=["security", "observability"],
            gates_required=["final-approval"],
            auto_approve=True,
        )
        results = [
            AgentResult(
                agent_name="security",
                category="security",
                files_generated=["rbac.yaml", "security-context.yaml"],
                success=True,
                findings_count=2,
            ),
            AgentResult(
                agent_name="observability",
                category="observability",
                files_generated=["dashboards.json"],
                success=True,
                findings_count=1,
            ),
        ]

        rec = orch._generate_recommendation(plan, results, [])
        assert rec.startswith("AUTO-APPROVED")
