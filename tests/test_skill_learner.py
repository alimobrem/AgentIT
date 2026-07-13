"""Tests for the skill learner watcher — the automatic counterpart to the
manual `agentit learn` CLI command and the portal's learn button."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agentit.cli import main
from agentit.watchers.skill_learner import SkillLearner


def _learner(**kwargs) -> tuple[SkillLearner, MagicMock]:
    publisher = MagicMock()
    learner = SkillLearner(publisher=publisher, **kwargs)
    return learner, publisher


def test_research_once_generates_new_skill():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00001"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=False), \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: cve-2099-00001\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/cve-2099-00001.md")):
        saved, skipped = learner.research_once()

    assert saved == ["cve-2099-00001"]
    assert skipped == []
    publisher.publish.assert_called_once()
    _, kwargs = publisher.publish.call_args
    assert kwargs["action"] == "skills-generated"
    assert "cve-2099-00001" in kwargs["summary"]


def test_research_once_skips_existing_skill():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00002"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=True):
        saved, skipped = learner.research_once()

    assert saved == []
    assert skipped == ["CVE-2099-00002"]
    publisher.publish.assert_not_called()


def test_research_once_no_llm_returns_empty_without_raising():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
        saved, skipped = learner.research_once()

    assert saved == []
    assert skipped == []
    publisher.publish.assert_not_called()


def test_research_once_no_research_results():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[]):
        saved, skipped = learner.research_once()

    assert saved == []
    assert skipped == []
    publisher.publish.assert_not_called()


def test_research_once_prioritizes_flagged_skill_over_cve_sweep(tmp_path):
    """Regression: the research cycle must check get_low_effectiveness_skills()
    first and research a replacement for the flagged skill instead of the
    generic CVE sweep -- this is the wiring that closes the self-improvement
    loop end to end."""
    from conftest import make_store

    skills_dir = tmp_path / "skills"
    (skills_dir / "security").mkdir(parents=True)
    (skills_dir / "security" / "network-policy.md").write_text(
        "---\nname: network-policy\ndomain: security\nversion: 1\n"
        "triggers: [network]\noutputs: [NetworkPolicy]\nstatus: active\n---\nbody\n",
        encoding="utf-8",
    )

    store = make_store()
    for _ in range(4):
        store.record_skill_outcome("network-policy", "app-a", "rejected", "wrong")
    store.record_skill_outcome("network-policy", "app-b", "rejected", "wrong")

    learner, publisher = _learner(store=store, skills_dir=skills_dir)

    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves") as mock_cves, \
         patch("agentit.learning_agent.research_skill_improvement",
               return_value={"title": "network-policy-v2", "description": "better"}) as mock_improve, \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: network-policy-v2\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/network-policy-v2.md")):
        saved, skipped = learner.research_once()

    mock_improve.assert_called_once()
    args, _ = mock_improve.call_args
    assert args[1] == "network-policy"
    assert args[2] == "security"
    mock_cves.assert_not_called()
    assert saved == ["network-policy-v2"]
    assert skipped == []


def test_research_once_falls_back_to_cve_sweep_when_nothing_flagged():
    """No low-effectiveness skills -> the existing CVE-sweep behavior runs
    exactly as before."""
    from conftest import make_store

    store = make_store()
    learner, publisher = _learner(store=store)

    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00009"}]) as mock_cves, \
         patch("agentit.learning_agent.research_skill_improvement") as mock_improve, \
         patch("agentit.learning_agent.check_skill_exists", return_value=False), \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: cve-2099-00009\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/cve-2099-00009.md")):
        saved, skipped = learner.research_once()

    mock_improve.assert_not_called()
    mock_cves.assert_called_once()
    assert saved == ["cve-2099-00009"]


def test_learn_watch_cli_options_registered():
    runner = CliRunner()
    result = runner.invoke(main, ["learn-watch", "--help"])
    assert result.exit_code == 0
    assert "--interval" in result.output
    assert "--limit" in result.output


def test_accepts_optional_store_for_tick_telemetry():
    from conftest import make_store
    store = make_store()
    learner, _ = _learner(store=store)
    assert learner._store is store


def test_defaults_to_none_store_when_omitted():
    learner, _ = _learner()
    assert learner._store is None


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.skill_learner.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        learner, _ = _learner()
        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
            await learner.run()

        captured = capsys.readouterr()
        assert "Starting skill learner" in captured.err
        assert "Skill learner stopped." in captured.err
        mock_sleep.assert_called_once_with(86400)


class TestTickRunsOffEventLoop:
    """research_once must be dispatched via asyncio.to_thread so it doesn't
    block the event loop for the tick's full duration, and record_tick
    telemetry must still fire afterwards."""

    @patch("agentit.watchers.skill_learner.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_research_once_dispatched_via_to_thread_and_telemetry_records(self, mock_sleep):
        from conftest import make_store
        store = make_store()
        learner, _ = _learner(store=store)

        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
             patch(
                 "agentit.watchers.skill_learner.asyncio.to_thread", wraps=asyncio.to_thread
             ) as mock_to_thread:
            await learner.run()

        mock_to_thread.assert_called_once_with(learner.research_once)
        events = store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)
