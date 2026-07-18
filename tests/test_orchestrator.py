from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

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
from conftest import make_async_store, make_report, make_store


class TestPlanSelectsAgents:
    def test_plan_has_no_skill_only_domains(self, tmp_path: Path) -> None:
        """security/observability/cicd/compliance/infrastructure/release/
        incident/retirement/chaos are now skill-only domains (see
        docs/agent-removal-readiness.md) -- their Python agents were
        removed, so they must never appear in plan.agents_to_run (skills
        still cover them, unconditionally, in Step 1 of run())."""
        for crit in ("low", "medium", "high", "critical"):
            report = make_report(criticality=crit)
            orch = FleetOrchestrator(report, tmp_path / f"out-{crit}")
            plan = orch.plan()
            for agent in ("security", "observability", "cicd", "compliance",
                          "infrastructure", "release", "incident", "retirement", "chaos"):
                assert agent not in plan.agents_to_run, f"{agent} unexpectedly planned for {crit}"

    def test_plan_adds_extra_agents_for_high_criticality(self, tmp_path: Path) -> None:
        """High criticality -> adds dependency, cost."""
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()

        for agent in ("dependency", "cost"):
            assert agent in plan.agents_to_run

    def test_plan_adds_codechange_for_high_criticality(self, tmp_path: Path) -> None:
        """High criticality -> codechange agent included."""
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()
        assert "codechange" in plan.agents_to_run

    def test_plan_adds_codechange_for_low_score(self, tmp_path: Path) -> None:
        """Score < 50 -> codechange agent included."""
        report = make_report()
        report.overall_score = 40.0
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()
        assert "codechange" in plan.agents_to_run

    def test_plan_skips_codechange_for_high_score_low_crit(self, tmp_path: Path) -> None:
        """Low criticality, score >= 50 -> no codechange."""
        report = make_report(criticality="low")
        report.overall_score = 80.0
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()
        assert "codechange" not in plan.agents_to_run

    def test_gates_required_no_longer_varies_by_criticality(self, tmp_path: Path) -> None:
        """`gates_required` (`_determine_gates()`) was never wired to
        `store.create_gate()` or any delivery check -- the `high`/`critical`
        "deploy-approval" entry it used to add was dead code implying a
        gate that was never actually created. Criticality's one real
        gating effect on delivery is `auto_approve` (see TestAutoApprove
        below), which is unaffected by this."""
        for crit in ("low", "medium", "high", "critical"):
            report = make_report(criticality=crit)
            orch = FleetOrchestrator(report, tmp_path / f"gates-{crit}")
            plan = orch.plan()
            assert "deploy-approval" not in plan.gates_required


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
    async def test_run_executes_agents(self, tmp_path: Path) -> None:
        """Run with a real report -> results have success=True for available agents.

        security/observability/cicd/compliance are now skill-only domains
        (see docs/agent-removal-readiness.md) -- with criticality="medium"
        and this report's generic finding, there are no Python agents left
        to plan at all, so this just guards that run() completes cleanly
        and doesn't resurrect a removed domain as a Python agent result.
        """
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        assert all(r.success for r in result.agent_results)
        agent_names = {r.agent_name for r in result.agent_results}
        for removed in ("security", "observability", "cicd", "compliance"):
            assert removed not in agent_names

    async def test_run_writes_summary(self, tmp_path: Path) -> None:
        """Verify orchestration-summary.md exists after run."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        await orch.run()

        summary_path = tmp_path / "out" / "orchestration-summary.md"
        assert summary_path.exists()
        content = summary_path.read_text()
        assert "Orchestration Summary" in content
        assert "test-app" in content

    async def test_run_records_structured_agent_runs(self, tmp_path: Path) -> None:
        """Every local agent execution must write a real agent_runs row so
        get_agent_stats()/list_agent_runs() reflect actual history, not a
        LIKE-matched heuristic over unrelated events."""
        # "cost" instead of the old "security" -- security is now a
        # skill-only domain with no Python agent left to run (see
        # docs/agent-removal-readiness.md).
        store, raw = await make_async_store()
        report = make_report(criticality="medium")
        aid = await raw.save(report)
        orch = FleetOrchestrator(
            report, tmp_path / "out", store=store, assessment_id=aid,
            agent_filter=["cost"],
        )
        await orch.run()

        runs = await raw.list_agent_runs("cost")
        assert len(runs) == 1
        assert runs[0]["status"] == "success"
        assert runs[0]["mode"] == "local"
        assert runs[0]["assessment_id"] == aid
        assert runs[0]["duration_ms"] is not None

        stats = await raw.get_agent_stats("cost")
        assert stats[0]["successes"] == 1


class TestDefaultSlosDedup:
    """Regression, confirmed live: re-onboarding an app (assess again,
    onboard again) re-ran `_create_default_slos()` with no uniqueness
    check, inserting a second full set of the same 3 default metrics on
    top of the existing ones -- 6 SLO rows instead of 3, inflating the
    Fleet-Wide SLOs page's "Total SLOs" stat. The fix skips any metric
    that already has an SLO for this assessment.
    """

    async def test_running_orchestrator_twice_does_not_duplicate_default_slos(self, tmp_path: Path) -> None:
        store, raw = await make_async_store()
        report = make_report(criticality="medium")
        aid = await raw.save(report)

        orch = FleetOrchestrator(report, tmp_path / "out1", store=store, assessment_id=aid)
        await orch.run()
        first_slos = await raw.list_slos(aid)
        assert len(first_slos) == 3

        # Re-onboard: a second orchestrator run against the *same*
        # assessment_id, exactly what re-clicking "Onboard This App" does.
        orch2 = FleetOrchestrator(report, tmp_path / "out2", store=store, assessment_id=aid)
        await orch2.run()

        slos = await raw.list_slos(aid)
        assert len(slos) == 3, f"expected 3 deduped default SLOs, got {len(slos)}: {slos}"
        metric_names = [s["metric_name"] for s in slos]
        assert sorted(metric_names) == sorted(set(metric_names)), "duplicate metric_name rows present"

    async def test_manually_added_slo_for_a_default_metric_is_not_duplicated_either(self, tmp_path: Path) -> None:
        """A manually-added SLO (Add SLO form) for one of the 3 default
        metrics must also block that metric's default from being seeded
        again -- the dedup check is keyed on metric_name alone, not on
        "was this row created by the orchestrator"."""
        store, raw = await make_async_store()
        report = make_report(criticality="medium")
        aid = await raw.save(report)
        await raw.save_slo(aid, "availability", 99.99)  # manual, before onboarding

        orch = FleetOrchestrator(report, tmp_path / "out", store=store, assessment_id=aid)
        await orch.run()

        slos = await raw.list_slos(aid)
        availability_rows = [s for s in slos if s["metric_name"] == "availability"]
        assert len(availability_rows) == 1
        assert availability_rows[0]["target_value"] == 99.99  # untouched, not overwritten
        assert len(slos) == 3  # availability (manual) + error_rate + latency_p99_ms (seeded)


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

    def test_no_false_conflict_when_agents_just_both_succeed(self, tmp_path: Path) -> None:
        """Regression: two agents both succeeding with unrelated, non-colliding
        output is NOT a conflict. Before the fix, PRIORITY_MATRIX pairs like
        (security, cicd) were flagged as 'priority' conflicts merely because
        both agents succeeded, making warnings non-empty on almost every run."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        results = [
            AgentResult(agent_name="security", category="security",
                        files_generated=["rbac.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="cicd", category="cicd",
                        files_generated=["pipeline.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="observability", category="observability",
                        files_generated=["dashboards.json"], success=True, findings_count=1),
            AgentResult(agent_name="compliance", category="compliance",
                        files_generated=["audit-policy.yaml"], success=True, findings_count=1),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert priority == [], f"Unexpected false-positive conflicts: {priority}"

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

    def test_path_collision_is_a_real_conflict(self, tmp_path: Path) -> None:
        """Two agents writing a file at the same output path IS a real conflict."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        results = [
            AgentResult(agent_name="security", category="security",
                        files_generated=["policy.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="cicd", category="cicd",
                        files_generated=["policy.yaml"], success=True, findings_count=1),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert len(priority) == 1
        assert set(priority[0]["agents"]) == {"security", "cicd"}
        assert priority[0]["winner"] == "security"

    def test_vpa_auto_mode_conflicts_with_hpa(self, tmp_path: Path) -> None:
        """A VPA actively resizing (updateMode != Off) alongside an HPA for
        the same workload IS a real conflict. The HPA is generated by the
        `hpa` skill (skills/infrastructure/hpa.md) under the aggregate
        `skills` AgentResult now that `infrastructure` is a skill-only
        domain, not a Python agent -- see KNOWN_KIND_CONFLICTS."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        cost_dir = tmp_path / "out" / "cost"
        cost_dir.mkdir(parents=True)
        (cost_dir / "resource-recommendations.yaml").write_text(
            "apiVersion: autoscaling.k8s.io/v1\n"
            "kind: VerticalPodAutoscaler\n"
            "metadata:\n  name: test-app-vpa\n"
            "spec:\n  updatePolicy:\n    updateMode: Auto\n"
        )
        skills_dir = tmp_path / "out" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "hpa.yaml").write_text(
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "metadata:\n  name: test-app-hpa\n"
            "spec: {}\n"
        )

        results = [
            AgentResult(agent_name="cost", category="cost",
                        files_generated=["resource-recommendations.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="skills", category="skills",
                        files_generated=["hpa.yaml"], success=True, findings_count=1),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert len(priority) == 1
        assert set(priority[0]["agents"]) == {"cost", "skills"}
        assert priority[0]["winner"] == "skills"

    def test_vpa_off_mode_does_not_conflict_with_hpa(self, tmp_path: Path) -> None:
        """A VPA in 'Off' (recommendation-only) mode does not actually fight
        the HPA for control, so it must not be flagged as a real conflict.
        This matches production behavior: CostOptimizationAgent always sets
        updateMode='Off' for high/critical apps, which is exactly when the
        `hpa` skill's output also matters most."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        cost_dir = tmp_path / "out" / "cost"
        cost_dir.mkdir(parents=True)
        (cost_dir / "resource-recommendations.yaml").write_text(
            "apiVersion: autoscaling.k8s.io/v1\n"
            "kind: VerticalPodAutoscaler\n"
            "metadata:\n  name: test-app-vpa\n"
            "spec:\n  updatePolicy:\n    updateMode: 'Off'\n"
        )
        skills_dir = tmp_path / "out" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "hpa.yaml").write_text(
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "metadata:\n  name: test-app-hpa\n"
            "spec: {}\n"
        )

        results = [
            AgentResult(agent_name="cost", category="cost",
                        files_generated=["resource-recommendations.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="skills", category="skills",
                        files_generated=["hpa.yaml"], success=True, findings_count=1),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert priority == []

    async def test_full_run_with_standard_profile_has_no_false_conflicts(self, tmp_path: Path) -> None:
        """Realistic multi-agent successful run must not produce any
        priority conflict warnings. criticality="high" so dependency/cost/
        codechange are actually planned -- security/observability/cicd/
        compliance/infrastructure/release are now skill-only domains and no
        longer appear in plan.agents_to_run (see
        docs/agent-removal-readiness.md)."""
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        successful = [r for r in result.agent_results if r.success]
        assert len(successful) >= 3

        priority = [c for c in result.conflicts if c["type"] == "priority"]
        assert priority == [], f"Unexpected false-positive conflicts: {priority}"

    async def test_auto_approve_downgraded_by_real_conflict(self, tmp_path: Path) -> None:
        """plan.auto_approve is computed from score/criticality alone at plan()
        time -- but a real conflict found during this actual run must still
        force auto_approve to False in the returned OrchestrationResult."""
        report = make_report(criticality="low")
        report.overall_score = 90.0
        orch = FleetOrchestrator(report, tmp_path / "out")

        # Sanity check: score/criticality alone would auto-approve.
        assert orch.plan().auto_approve is True

        sec_dir = tmp_path / "out" / "security"
        sec_dir.mkdir(parents=True)
        (sec_dir / "shared.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: shared\n"
        )
        cicd_dir = tmp_path / "out" / "cicd"
        cicd_dir.mkdir(parents=True)
        (cicd_dir / "shared.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: shared\n"
        )
        colliding_results = [
            AgentResult(agent_name="security", category="security",
                        files_generated=["shared.yaml"], success=True, findings_count=1),
            AgentResult(agent_name="cicd", category="cicd",
                        files_generated=["shared.yaml"], success=True, findings_count=1),
        ]

        with patch.object(orch, "_run_agents_local", AsyncMock(return_value=colliding_results)):
            result = await orch.run()

        assert any(c["type"] == "priority" for c in result.conflicts)
        assert result.plan.auto_approve is False


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

    async def test_cost_agent_runs_for_high_criticality(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        agent_names = {r.agent_name for r in result.agent_results}
        assert "cost" in agent_names

        cost_result = [r for r in result.agent_results if r.agent_name == "cost"][0]
        assert cost_result.success is True
        assert cost_result.files_generated

    def test_all_optional_agents_importable(self) -> None:
        """Every optional agent module imports — catches class name mismatches.

        incident/chaos/retirement were removed once skills covered their
        domains (see docs/agent-removal-readiness.md) -- dependency/cost/
        codechange are the ones left.
        """
        from agentit.agents.dependency import DependencyAgent
        from agentit.agents.cost import CostOptimizationAgent
        from agentit.agents.codechange import CodeChangeAgent
        assert all([DependencyAgent, CostOptimizationAgent, CodeChangeAgent])


class TestNoSilentSwallowing:
    """Regression: orchestrator must never silently skip agents."""

    async def test_planned_agents_all_appear_in_results(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        planned = set(result.plan.agents_to_run)
        reported = {r.agent_name for r in result.agent_results}
        # "skills" is an extra, always-possible result on top of whatever
        # Python agents were planned -- it isn't itself a planned agent (see
        # FleetOrchestrator.run() Step 1, which runs skills unconditionally
        # before Step 2 plans/skips Python agents).
        reported.discard("skills")
        assert planned == reported, f"Planned {planned} but only got results for {reported}"

    async def test_missing_agent_logged_as_failure(self, tmp_path: Path, caplog) -> None:
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
            result = await orch.run()

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

    async def test_validation_adds_conflict(self, tmp_path: Path) -> None:
        """Full run with all valid agents should not produce validation conflicts."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        validation_conflicts = [c for c in result.conflicts if c["type"] == "validation"]
        assert validation_conflicts == [], f"Unexpected validation failures: {validation_conflicts}"


class TestSkillsFirstLLMPassthrough:
    """Regression: the orchestrator constructed SkillEngine/run_all() without
    ever passing an LLM client, even when credentials were configured -- so
    LLM-only skills (mode: llm, no template block) silently produced nothing
    in every portal/webhook onboarding flow. This test uses a report whose
    finding actually matches a real skill's triggers (unlike the other tests
    in this file, which use category="test" and never match any skill)."""

    async def test_run_constructs_and_forwards_llm_client_to_skill_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

        report = make_report(criticality="medium")
        report.scores[0].findings[0].category = "network"
        report.scores[0].findings[0].description = "Missing network isolation between pods"

        fake_llm = MagicMock()
        fake_llm._chat.return_value = (
            "apiVersion: networking.k8s.io/v1\n"
            "kind: NetworkPolicy\n"
            "metadata:\n  name: test-app-netpol\n"
            "spec:\n  podSelector: {}\n  policyTypes:\n    - Ingress\n    - Egress\n"
        )

        with patch("agentit.llm.LLMClient", return_value=fake_llm) as mock_llm_cls, \
             patch("agentit.platform_context.discover_platform", side_effect=RuntimeError("no cluster in tests")):
            orch = FleetOrchestrator(report, tmp_path / "out")
            result = await orch.run()

        mock_llm_cls.assert_called_once()
        assert fake_llm._chat.called, "SkillEngine never used the LLM client the orchestrator built"

        skills_result = [r for r in result.agent_results if r.agent_name == "skills"]
        assert len(skills_result) == 1
        assert any("network-policy" in f for f in skills_result[0].files_generated)

    async def test_run_falls_back_gracefully_when_llm_init_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LLM construction failures must never break skills-first generation
        or the orchestration run as a whole. criticality="high" so
        dependency/cost/codechange actually run -- with "medium" (and this
        report's generic finding), there would be no Python agents left to
        plan at all (see docs/agent-removal-readiness.md), which would make
        this assertion vacuous rather than a real regression guard."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

        report = make_report(criticality="high")

        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
            orch = FleetOrchestrator(report, tmp_path / "out")
            result = await orch.run()  # must not raise

        assert result.agent_results  # run still completed
