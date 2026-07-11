"""End-to-end workflow tests — verify the full pipelines work."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import make_store, make_report

from agentit.models import DimensionScore, Finding, Severity


class TestAssessOnboardFlow:
    """Assess → onboard → manifests generated → remediations recorded."""

    def test_assess_produces_report(self, tmp_path):
        from agentit.runner import run_assessment

        repo = tmp_path / "app"
        repo.mkdir()
        (repo / "main.py").write_text("print('hello')\n")
        report = run_assessment(repo, "https://github.com/org/app", "medium")
        assert report.overall_score >= 0
        assert len(report.scores) == 7

    def test_onboard_generates_manifests(self, tmp_path):
        from agentit.agents.orchestrator import FleetOrchestrator

        report = make_report(criticality="high")
        store = make_store()
        aid = store.save(report)

        orch = FleetOrchestrator(report=report, output_dir=tmp_path / "out",
                                 store=store, assessment_id=aid)
        result = orch.run()

        assert any(r.success for r in result.agent_results)
        assert len(store.list_remediations(aid)) > 0
        assert len(store.list_slos(aid)) > 0

    def test_orchestrator_registers_agents(self, tmp_path):
        from agentit.agents.orchestrator import FleetOrchestrator

        store = make_store()
        report = make_report()
        aid = store.save(report)

        orch = FleetOrchestrator(report=report, output_dir=tmp_path / "out",
                                 store=store, assessment_id=aid)
        orch.run()

        agents = store.list_agents()
        assert len(agents) >= 5


class TestAutoModeFlow:
    """Auto-mode: should_auto_apply decision matrix."""

    def test_disabled_always_gates(self):
        from agentit.automode import AutoMode
        store = make_store()
        auto = AutoMode(store=store)
        ok, _ = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False

    def test_enabled_no_llm_gates(self):
        from agentit.automode import AutoMode
        store = make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store, llm_client=None)
        ok, _ = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False

    def test_enabled_llm_safe_approves(self):
        from agentit.automode import AutoMode
        store = make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "safe"}
        auto = AutoMode(store=store, llm_client=llm)
        ok, _ = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is True

    def test_enabled_llm_destructive_gates(self):
        from agentit.automode import AutoMode
        store = make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": True, "confidence": 0.9, "reason": "deletes"}
        auto = AutoMode(store=store, llm_client=llm)
        ok, _ = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False


class TestRemediationLoopFlow:
    """Remediation loop: assess failure doesn't crash."""

    def test_loop_handles_assess_failure(self):
        from agentit.remediation_loop import RemediationLoop
        store = make_store()
        loop = RemediationLoop(portal_url="http://bad-host:9999", store=store, timeout=2)
        result = loop.trigger("https://github.com/org/app", "app", reason="test")
        assert result["outcome"] == "failed"
        assert result["step"] == "assess"
        loop.close()

    def test_loop_logs_events(self):
        from agentit.remediation_loop import RemediationLoop
        store = make_store()
        loop = RemediationLoop(portal_url="http://bad-host:9999", store=store, timeout=2)
        loop.trigger("https://github.com/org/app", "test-app", reason="test")
        events = store.list_events()
        assert any(e["action"] == "loop-started" for e in events)
        loop.close()


class TestInfraRepoFlow:
    """Infra repo: commit_to_infra_repo lowercases app name."""

    def test_app_name_lowercased(self):
        from agentit.portal.github_pr import commit_to_infra_repo
        with patch("agentit.portal.github_pr._get_token", return_value="fake"):
            with patch("agentit.portal.github_pr.requests") as mock_req:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"default_branch": "main", "object": {"sha": "abc"}, "sha": "def", "html_url": "http://x"}
                mock_resp.raise_for_status = MagicMock()
                mock_resp.text = ""
                mock_req.get.return_value = mock_resp
                mock_req.post.return_value = mock_resp

                result = commit_to_infra_repo(
                    "https://github.com/org/gitops",
                    "MyApp",
                    [{"category": "sec", "path": "rbac.yaml", "content": "x"}],
                )

                # Verify the tree path uses lowercase
                post_calls = mock_req.post.call_args_list
                tree_call = [c for c in post_calls if "trees" in str(c)]
                if tree_call:
                    tree_items = tree_call[0][1]["json"]["tree"]
                    assert tree_items[0]["path"].startswith("apps/myapp/")


class TestGateLifecycleFlow:
    """Gates: dedup, expire, cascade delete."""

    def test_full_gate_lifecycle(self):
        store = make_store()
        aid = store.save(make_report())

        g1 = store.create_gate(aid, "deploy", "Approve")
        g2 = store.create_gate(aid, "deploy", "Duplicate")
        assert g1 == g2

        store.resolve_gate(g1, "approved", "user")
        assert len(store.list_gates("pending")) == 0
        assert len(store.list_gates("approved")) == 1

    def test_delete_cascades_gates(self):
        store = make_store()
        aid = store.save(make_report())
        store.create_gate(aid, "deploy", "Gate")
        store.save_remediation(aid, "security", "Fix")
        store.save_slo(aid, "avail", 99.9)

        store.delete(aid)
        assert store.get(aid) is None
        assert store.list_remediations(aid) == []
        assert store.list_slos(aid) == []


class TestCodeChangeAgentFlow:
    """Code change agent: generates patches for actionable findings."""

    def test_generates_dockerfile_for_container_finding(self, tmp_path):
        from agentit.agents.codechange import CodeChangeAgent
        report = make_report(
            scores=[DimensionScore(
                dimension="security", score=30, max_score=100,
                findings=[Finding(category="container", severity=Severity.high,
                                  description="No Dockerfile", recommendation="Add it")],
            )],
        )
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert "USER 1001" in result.changes[0].content

    def test_skips_non_actionable_findings(self, tmp_path):
        from agentit.agents.codechange import CodeChangeAgent
        report = make_report(
            scores=[DimensionScore(
                dimension="security", score=30, max_score=100,
                findings=[Finding(category="network", severity=Severity.high,
                                  description="No NetworkPolicy", recommendation="Add it")],
            )],
        )
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 0
