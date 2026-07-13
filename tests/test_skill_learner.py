"""Tests for the skill learner watcher — the automatic counterpart to the
manual `agentit learn` CLI command and the portal's learn button."""
from __future__ import annotations

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
