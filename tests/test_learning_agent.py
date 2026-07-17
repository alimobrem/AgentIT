"""Tests for learning_agent.py's pure helpers -- primarily
describe_learning_run(), the shared logic behind the "every learning run
leaves a durable trace" transparency work shared by the portal's
"Research CVEs & Generate Skills" button (routes/capabilities.py) and the
skill-learner watcher (watchers/skill_learner.py)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agentit.learning_agent import (
    LEARNING_RUN_ACTION,
    count_recent_improvement_failures,
    describe_learning_run,
)


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


def _run_event(*, hours_ago: float, mode: str | None, skipped: list[str]) -> dict:
    """Build one raw event dict in the exact shape
    ``AssessmentStore.list_events_by_action()`` returns -- ``timestamp`` an
    ISO-8601 string, ``details_json`` a JSON string (not pre-parsed)."""
    timestamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    details = {"trigger": "watcher", "mode": mode, "saved": [], "skipped": skipped}
    return {"timestamp": timestamp, "details_json": json.dumps(details)}


class TestCountRecentImprovementFailures:
    """Read side of the stuck-loop fix -- the same flagged skill kept
    failing to improve "couldn't be improved this time" on every single
    tick with zero cooldown/backoff logic. This reconstructs a per-skill
    recent-failure count from ``learning-run`` history alone, with no new
    persisted attempts table."""

    def test_counts_skipped_skill_improvement_entries_within_window(self):
        events = [
            _run_event(hours_ago=1, mode="skill-improvement", skipped=["network-policy"]),
            _run_event(hours_ago=2, mode="skill-improvement", skipped=["network-policy"]),
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        counts = count_recent_improvement_failures(events, cutoff)

        assert counts == {"network-policy": 2}

    def test_ignores_events_older_than_cutoff(self):
        events = [
            _run_event(hours_ago=1, mode="skill-improvement", skipped=["network-policy"]),
            _run_event(hours_ago=48, mode="skill-improvement", skipped=["network-policy"]),
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        counts = count_recent_improvement_failures(events, cutoff)

        assert counts == {"network-policy": 1}

    def test_ignores_cve_sweep_mode_entries(self):
        """A skipped CVE (already has a matching skill) is unrelated to the
        skill-improvement cooldown -- must not count towards it."""
        events = [_run_event(hours_ago=1, mode="cve-sweep", skipped=["CVE-2099-0001"])]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        counts = count_recent_improvement_failures(events, cutoff)

        assert counts == {}

    def test_ignores_events_with_no_skipped_skills(self):
        events = [_run_event(hours_ago=1, mode="skill-improvement", skipped=[])]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        assert count_recent_improvement_failures(events, cutoff) == {}

    def test_tolerates_malformed_events(self):
        """A missing timestamp or unparseable details_json must be skipped,
        not raised on -- this reads store history, which should never be
        able to crash a research tick."""
        events = [
            {"timestamp": None, "details_json": "{}"},
            {"timestamp": "not-a-date", "details_json": "{}"},
            {"timestamp": datetime.now(timezone.utc).isoformat(), "details_json": "not json"},
            _run_event(hours_ago=1, mode="skill-improvement", skipped=["network-policy"]),
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        assert count_recent_improvement_failures(events, cutoff) == {"network-policy": 1}
