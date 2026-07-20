"""End-to-end workflow tests — verify the full pipelines work."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import make_async_store, make_store, make_report

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

    async def test_onboard_generates_manifests(self, tmp_path):
        from agentit.agents.orchestrator import FleetOrchestrator

        report = make_report(criticality="high")
        store, raw = await make_async_store()
        aid = await raw.save(report)

        orch = FleetOrchestrator(report=report, output_dir=tmp_path / "out",
                                 store=store, assessment_id=aid)
        result = await orch.run()

        assert any(r.success for r in result.agent_results)
        assert any(r.files_generated for r in result.agent_results)
        assert len(await raw.list_slos(aid)) > 0

    async def test_orchestrator_registers_agents(self, tmp_path):
        """security/observability/cicd/compliance/infrastructure/incident/
        release/retirement/chaos were removed once skills covered their
        domains (see docs/agent-removal-readiness.md) -- only 3 Python
        agents are left to register."""
        from agentit.agents.orchestrator import FleetOrchestrator

        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)

        orch = FleetOrchestrator(report=report, output_dir=tmp_path / "out",
                                 store=store, assessment_id=aid)
        await orch.run()

        agents = await raw.list_agents()
        assert len(agents) >= 3


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


class TestDeleteCascade:
    """Delete removes every dependent record for the assessment."""

    async def test_delete_cascades_slos(self):
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_slo(aid, "avail", 99.9)

        await store.delete(aid)
        assert await store.get(aid) is None
        assert await store.list_slos(aid) == []


class TestCodeChangeAgentFlow:
    """Code change agent: generates patches for actionable findings."""

    def test_generates_gitignore_for_finding(self, tmp_path):
        from agentit.agents.codechange import CodeChangeAgent
        report = make_report(
            scores=[DimensionScore(
                dimension="security", score=30, max_score=100,
                findings=[Finding(category="gitignore", severity=Severity.low,
                                  description="Missing .gitignore", recommendation="Add it")],
            )],
        )
        agent = CodeChangeAgent(report, tmp_path / "out")
        result = agent.run()
        assert len(result.changes) == 1
        assert ".env" in result.changes[0].content

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
