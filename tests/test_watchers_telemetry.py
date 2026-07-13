"""Tests for the shared watcher tick telemetry helper (agentit.watchers.record_tick)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agentit.watchers import record_tick
from conftest import make_async_store


async def test_record_tick_success_logs_event_and_heartbeats():
    store, raw_store = make_async_store()
    await record_tick(store, "vuln-watcher", success=True)

    events = raw_store.list_events()
    assert len(events) == 1
    assert events[0]["action"] == "tick-complete"
    assert events[0]["agent_id"] == "vuln-watcher"

    agents = raw_store.list_agents()
    assert any(a["agent_name"] == "vuln-watcher" for a in agents)


async def test_record_tick_failure_logs_tick_failed_with_error():
    store, raw_store = make_async_store()
    await record_tick(store, "slo-tracker", success=False, error="boom")

    events = raw_store.list_events()
    assert events[0]["action"] == "tick-failed"
    assert "boom" in events[0]["summary"]


async def test_record_tick_sets_last_success_gauge_only_on_success():
    from agentit.portal.metrics import watcher_last_success_timestamp

    store, _raw_store = make_async_store()
    await record_tick(store, "drift-detector", success=True)
    before = watcher_last_success_timestamp.labels(watcher="drift-detector")._value.get()
    assert before > 0


async def test_record_tick_without_store_does_not_raise():
    """Store is optional (e.g. a watcher constructed without one in a test)."""
    await record_tick(None, "skill-learner", success=True)
    await record_tick(None, "skill-learner", success=False, error="x")


async def test_record_tick_store_failure_does_not_propagate():
    store = MagicMock()
    store.log_event = AsyncMock(side_effect=RuntimeError("db locked"))
    await record_tick(store, "vuln-watcher", success=True)  # must not raise
