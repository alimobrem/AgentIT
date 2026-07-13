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
