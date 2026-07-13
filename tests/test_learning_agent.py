"""Tests for learning_agent.py's pure helpers -- primarily
describe_learning_run(), the shared logic behind the "every learning run
leaves a durable trace" transparency work shared by the portal's
"Research CVEs & Generate Skills" button (routes/capabilities.py) and the
skill-learner watcher (watchers/skill_learner.py)."""
from __future__ import annotations

from agentit.learning_agent import LEARNING_RUN_ACTION, describe_learning_run


def test_action_name_is_stable():
    """Both entry points must log under the same action string so a single
    list_events_by_action() query surfaces both manual and watcher runs."""
    assert LEARNING_RUN_ACTION == "learning-run"


def test_saved_skills_from_cve_sweep():
    severity, summary, details = describe_learning_run("manual", "cve-sweep", ["cve-2099-1"], [])
    assert severity == "info"
    assert "new skill" in summary
    assert "cve-2099-1" in summary
    assert details == {"trigger": "manual", "mode": "cve-sweep", "saved": ["cve-2099-1"], "skipped": []}


def test_saved_skills_from_skill_improvement_uses_improvement_wording():
    severity, summary, _details = describe_learning_run("watcher", "skill-improvement", ["network-policy-v2"], [])
    assert severity == "info"
    assert "improvement" in summary
    assert "network-policy-v2" in summary


def test_saved_and_skipped_mentions_both():
    _severity, summary, _details = describe_learning_run("manual", "cve-sweep", ["a"], ["b"])
    assert "a" in summary
    assert "1 skipped" in summary


def test_skipped_only_cve_sweep():
    severity, summary, _details = describe_learning_run("watcher", "cve-sweep", [], ["CVE-2099-2"])
    assert severity == "warning"
    assert "already have matching skills" in summary


def test_skipped_only_skill_improvement():
    severity, summary, _details = describe_learning_run("manual", "skill-improvement", [], ["network-policy"])
    assert severity == "warning"
    assert "couldn't be improved" in summary


def test_nothing_generated_and_nothing_skipped():
    severity, summary, details = describe_learning_run("watcher", "cve-sweep", [], [])
    assert severity == "warning"
    assert "nothing usable" in summary
    assert details["saved"] == []
    assert details["skipped"] == []


def test_error_takes_priority_over_saved_or_skipped():
    """An exception mid-run means nothing was actually saved to disk even if
    partial in-memory state exists -- the error message must win."""
    severity, summary, details = describe_learning_run("manual", "cve-sweep", [], [], error="LLM timed out")
    assert severity == "error"
    assert "LLM timed out" in summary
    assert details["error"] == "LLM timed out"


def test_mode_none_when_run_never_reached_a_mode():
    """The LLM-unavailable case never gets far enough to know whether it
    would have been a skill-improvement pass or a CVE sweep."""
    severity, summary, details = describe_learning_run("manual", None, [], [], error="no credentials")
    assert severity == "error"
    assert details["mode"] is None
    assert "no credentials" in summary
