from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agentit.agents.orchestrator import (
    AgentResult,
    FleetOrchestrator,
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

    def test_plan_does_not_add_removed_cost_dependency_agents(self, tmp_path: Path) -> None:
        """Cost/dependency are skill-only — never planned as Python agents."""
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        plan = orch.plan()
        for agent in ("dependency", "cost"):
            assert agent not in plan.agents_to_run

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
        # codechange is the sole remaining Python onboarding agent.
        store, raw = await make_async_store()
        report = make_report(criticality="medium")
        aid = await raw.save(report)
        orch = FleetOrchestrator(
            report, tmp_path / "out", store=store, assessment_id=aid,
            agent_filter=["codechange"],
        )
        await orch.run()

        runs = await raw.list_agent_runs("codechange")
        assert len(runs) == 1
        assert runs[0]["status"] == "success"
        assert runs[0]["mode"] == "local"
        assert runs[0]["assessment_id"] == aid
        assert runs[0]["duration_ms"] is not None

        stats = await raw.get_agent_stats("codechange")
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
        # assessment_id, exactly what re-clicking Scan/retrying Onboard does.
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

    def test_vpa_and_hpa_under_skills_do_not_cross_agent_conflict(self, tmp_path: Path) -> None:
        """After cost-agent removal, VPA and HPA both come from the skills
        AgentResult — KNOWN_KIND_CONFLICTS is empty; no cross-agent pair."""
        report = make_report(criticality="medium")
        orch = FleetOrchestrator(report, tmp_path / "out")

        skills_dir = tmp_path / "out" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "vpa.yaml").write_text(
            "apiVersion: autoscaling.k8s.io/v1\n"
            "kind: VerticalPodAutoscaler\n"
            "metadata:\n  name: test-app-vpa\n"
            "spec:\n  updatePolicy:\n    updateMode: Auto\n"
        )
        (skills_dir / "hpa.yaml").write_text(
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "metadata:\n  name: test-app-hpa\n"
            "spec: {}\n"
        )

        results = [
            AgentResult(agent_name="skills", category="skills",
                        files_generated=["vpa.yaml", "hpa.yaml"], success=True, findings_count=2),
        ]
        conflicts = orch._detect_conflicts(results)
        priority = [c for c in conflicts if c["type"] == "priority"]
        assert priority == []

    async def test_full_run_with_standard_profile_has_no_false_conflicts(self, tmp_path: Path) -> None:
        """Realistic skills-primary run must not produce false priority
        conflicts. criticality=high plans optional codechange; skills run
        unconditionally."""
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        successful = [r for r in result.agent_results if r.success]
        assert len(successful) >= 1

        priority = [c for c in result.conflicts if c["type"] == "priority"]
        assert priority == [], f"Unexpected false-positive conflicts: {priority}"


class TestRecommendation:
    def test_recommendation_ready_for_review_when_all_succeed_no_conflicts(self, tmp_path: Path) -> None:
        """All agents succeed, no conflicts -> READY FOR REVIEW recommendation.

        `_generate_recommendation()` no longer has an `AUTO-APPROVED` branch
        (removed 2026-07-20 alongside `plan.auto_approve`, whose one real
        consumer, AutoMode, is fully removed) -- every successful run is now
        always READY FOR REVIEW, awaiting an explicit human Deliver click
        regardless of score/criticality."""
        report = make_report(criticality="low")
        report.overall_score = 80.0
        orch = FleetOrchestrator(report, tmp_path / "out")

        results = [
            AgentResult(
                agent_name="skills",
                category="skills",
                files_generated=["resource-recommendations.yaml"],
                success=True,
                findings_count=1,
            ),
        ]

        rec = orch._generate_recommendation(results, [])
        assert rec.startswith("READY FOR REVIEW")
        assert "skills generated" in rec

    def test_recommendation_notes_conflict_count(self, tmp_path: Path) -> None:
        report = make_report(criticality="low")
        orch = FleetOrchestrator(report, tmp_path / "out")

        results = [
            AgentResult(agent_name="skills", category="skills",
                        files_generated=["x.yaml"], success=True, findings_count=1),
        ]
        conflicts = [{"type": "priority", "agents": ["codechange", "skills"],
                      "resolution": "skills wins", "winner": "skills"}]

        rec = orch._generate_recommendation(results, conflicts)
        assert rec.startswith("READY FOR REVIEW")
        assert "1 conflict(s)" in rec


class TestCodechangeAgentRegistration:
    """codechange remains the optional source-patch Python agent."""

    async def test_codechange_runs_for_high_criticality(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        orch = FleetOrchestrator(report, tmp_path / "out")
        result = await orch.run()

        agent_names = {r.agent_name for r in result.agent_results}
        assert "codechange" in agent_names

        cc = [r for r in result.agent_results if r.agent_name == "codechange"][0]
        assert cc.success is True

    def test_codechange_agent_importable(self) -> None:
        from agentit.agents.codechange import CodeChangeAgent
        assert CodeChangeAgent is not None
        assert hasattr(CodeChangeAgent, "run")


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
