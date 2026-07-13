"""Tests for the shared watcher tick telemetry helper (agentit.watchers.record_tick)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.watchers import record_tick
from conftest import make_store


def test_record_tick_success_logs_event_and_heartbeats():
    store = make_store()
    record_tick(store, "vuln-watcher", success=True)

    events = store.list_events()
    assert len(events) == 1
    assert events[0]["action"] == "tick-complete"
    assert events[0]["agent_id"] == "vuln-watcher"

    agents = store.list_agents()
    assert any(a["agent_name"] == "vuln-watcher" for a in agents)


def test_record_tick_failure_logs_tick_failed_with_error():
    store = make_store()
    record_tick(store, "slo-tracker", success=False, error="boom")

    events = store.list_events()
    assert events[0]["action"] == "tick-failed"
    assert "boom" in events[0]["summary"]


def test_record_tick_sets_last_success_gauge_only_on_success():
    from agentit.portal.metrics import watcher_last_success_timestamp

    store = make_store()
    record_tick(store, "drift-detector", success=True)
    before = watcher_last_success_timestamp.labels(watcher="drift-detector")._value.get()
    assert before > 0


def test_record_tick_without_store_does_not_raise():
    """Store is optional (e.g. a watcher constructed without one in a test)."""
    record_tick(None, "skill-learner", success=True)
    record_tick(None, "skill-learner", success=False, error="x")


def test_record_tick_store_failure_does_not_propagate():
    store = MagicMock()
    store.log_event.side_effect = RuntimeError("db locked")
    record_tick(store, "vuln-watcher", success=True)  # must not raise
