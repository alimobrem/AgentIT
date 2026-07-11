from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agentit.agents.orchestrator import (
    AgentResult,
    FleetOrchestrator,
    OrchestrationPlan,
    PRIORITY_MATRIX,
)
from agentit.models import (
    DimensionScore,
    Finding,
    Severity,
)
from conftest import make_report


class TestPlanSelectsAgents:
    def test_plan_selects_core_agents(self, tmp_path: Path) -> None:
        """Medium criticality -> 5 core agents + chaos."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        for agent in ("security", "observability", "cicd", "compliance", "release"):
            assert agent in plan.agents_to_run
        assert "chaos" in plan.agents_to_run

    def test_release_agent_always_selected(self, tmp_path: Path) -> None:
        """Release coordinator is selected for all criticality levels."""
        for crit in ("low", "medium", "high", "critical"):
            report = make_report(criticality=crit)
            orch = FleetOrchestrator(report, tmp_path / f"out-{crit}")
            plan = orch.plan()
            assert "release" in plan.agents_to_run, f"release not selected for {crit}"

    def test_plan_adds_extra_agents_for_high_criticality(self, tmp_path: Path) -> None:
        """High criticality -> adds dependency, incident, cost."""
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        for agent in ("dependency", "incident", "cost"):
            assert agent in plan.agents_to_run

    def test_plan_skips_chaos_for_critical(self, tmp_path: Path) -> None:
        """Critical criticality -> no chaos."""
        report = make_report(criticality="critical")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        assert "chaos" not in plan.agents_to_run

    def test_plan_adds_retirement_for_low_score(self, tmp_path: Path) -> None:
        """Score < 30 -> retirement agent included."""
        report = make_report()
        report.overall_score = 20.0
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
        report = make_report(criticality="low", scores=scores)
        report.overall_score = 80.0
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
        report = make_report(criticality="low", scores=scores)
        report.overall_score = 80.0
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        assert plan.auto_approve is False


class TestRun:
    def test_run_executes_agents(self, tmp_path: Path) -> None:
        """Run with a real report -> results have success=True for available agents."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = orch.run()

        successful = [r for r in result.agent_results if r.success]
        assert len(successful) >= 4  # At least the 4 core agents

        agent_names = {r.agent_name for r in successful}
        for core in ("security", "observability", "cicd", "compliance"):
            assert core in agent_names

    def test_run_writes_summary(self, tmp_path: Path) -> None:
        """Verify orchestration-summary.md exists after run."""
        report = make_report(criticality="medium")
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
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

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
        blockers = [c for c in conflicts if c["type"] == "blocker"]
        assert len(blockers) == 1
        assert blockers[0]["winner"] == "security"

    def test_priority_matrix_resolves_security_over_cicd(self, tmp_path: Path) -> None:
        """When both security and cicd succeed, priority matrix picks security."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        results = [
            AgentResult(agent_name="security", category="security",
                        files_generated=["rbac.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="cicd", category="cicd",
                        files_generated=["pipeline.yaml"], success=True, findings_count=1),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert len(priority) >= 1
        sec_cicd = [c for c in priority if set(c["agents"]) == {"security", "cicd"}]
        assert len(sec_cicd) == 1
        assert sec_cicd[0]["winner"] == "security"

    def test_priority_matrix_not_applied_when_agent_failed(self, tmp_path: Path) -> None:
        """Priority conflicts only fire for agents that both succeeded."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        results = [
            AgentResult(agent_name="security", category="security",
                        files_generated=[], success=False, error="crashed"),
            AgentResult(agent_name="cicd", category="cicd",
                        files_generated=["pipeline.yaml"], success=True),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert priority == []


class TestRecommendation:
    def test_recommendation_blocked(self, tmp_path: Path) -> None:
        """Conflict present -> BLOCKED recommendation."""
        report = make_report(criticality="medium")
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
        report = make_report(criticality="low")
        report.overall_score = 80.0
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


class TestCostAgentRegistration:
    """Regression: CostAgent import was broken (wrong class name). Must never regress."""

    def test_cost_agent_runs_for_high_criticality(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = orch.run()

        agent_names = {r.agent_name for r in result.agent_results}
        assert "cost" in agent_names

        cost_result = [r for r in result.agent_results if r.agent_name == "cost"][0]
        assert cost_result.success is True
        assert cost_result.files_generated

    def test_all_optional_agents_importable(self) -> None:
        """Every optional agent module imports — catches class name mismatches."""
        from agentit.agents.dependency import DependencyAgent
        from agentit.agents.incident import IncidentAgent
        from agentit.agents.cost import CostOptimizationAgent
        from agentit.agents.chaos import ChaosAgent
        from agentit.agents.retirement import RetirementAgent
        assert all([DependencyAgent, IncidentAgent, CostOptimizationAgent, ChaosAgent, RetirementAgent])


class TestNoSilentSwallowing:
    """Regression: orchestrator must never silently skip agents."""

    def test_planned_agents_all_appear_in_results(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = orch.run()

        planned = set(result.plan.agents_to_run)
        reported = {r.agent_name for r in result.agent_results}
        assert planned == reported, f"Planned {planned} but only got results for {reported}"

    def test_missing_agent_logged_as_failure(self, tmp_path: Path, caplog) -> None:
        """If an agent is planned but not in agent_map, it shows as failure + log."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        # Inject a fake agent name into the plan
        original_select = orch._select_agents
        def _patched_select():
            agents = original_select()
            agents.append("nonexistent_agent")
            return agents
        orch._select_agents = _patched_select

        with caplog.at_level(logging.WARNING):
            result = orch.run()

        nonexistent = [r for r in result.agent_results if r.agent_name == "nonexistent_agent"]
        assert len(nonexistent) == 1
        assert nonexistent[0].success is False
        assert "not available" in nonexistent[0].error
        assert any("nonexistent_agent" in m for m in caplog.messages)


class TestPostHardeningValidation:
    def test_catches_missing_file(self, tmp_path: Path) -> None:
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        (tmp_path / "out" / "security").mkdir(parents=True)

        results = [AgentResult(
            agent_name="security", category="security",
            files_generated=["nonexistent.yaml"], success=True,
        )]
        issues = orch._post_hardening_validation(results)
        assert len(issues) == 1
        assert "missing from disk" in issues[0]

    def test_catches_invalid_yaml_on_disk(self, tmp_path: Path) -> None:
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        sec_dir = tmp_path / "out" / "security"
        sec_dir.mkdir(parents=True)
        (sec_dir / "bad.yaml").write_text("kind: Pod\n")  # missing apiVersion, metadata

        results = [AgentResult(
            agent_name="security", category="security",
            files_generated=["bad.yaml"], success=True,
        )]
        issues = orch._post_hardening_validation(results)
        assert len(issues) >= 1

    def test_validation_adds_conflict(self, tmp_path: Path) -> None:
        """Full run with all valid agents should not produce validation conflicts."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = orch.run()

        validation_conflicts = [c for c in result.conflicts if c["type"] == "validation"]
        assert validation_conflicts == [], f"Unexpected validation failures: {validation_conflicts}"
