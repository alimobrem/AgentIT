"""Tests for the vulnerability watcher — previously untested. Added
alongside Phase 3 of docs/postgres-migration-plan.md §9, which converted
``run()`` to ``async def`` (``time.sleep()`` -> ``await asyncio.sleep()``).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from agentit.watchers.vuln_watcher import VulnWatcher
from conftest import make_store


def _watcher(store=None, consumer=None) -> VulnWatcher:
    return VulnWatcher(
        publisher=MagicMock(),
        store=store or make_store(),
        consumer=consumer or MagicMock(),
        interval=1,
    )


async def test_check_fleet_with_empty_fleet_is_a_noop():
    watcher = _watcher()
    await watcher.check_fleet()  # must not raise, even with no tracked apps


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = _watcher(consumer=consumer)
        await watcher.run()

        captured = capsys.readouterr()
        assert "Starting vulnerability watcher" in captured.err
        assert "Vulnerability watcher stopped." in captured.err
        mock_sleep.assert_called_once_with(1)


class TestTickRunsOnEventLoop:
    """``check_fleet`` is now a genuine coroutine (this pass's
    FleetOrchestrator/AutoMode/RemediationDispatcher/RemediationLoop async
    rewrite made VulnWatcher's own AutoMode/RemediationLoop call sites
    async too) -- ``run()`` `await`s it directly rather than dispatching
    the whole tick to a worker thread via ``asyncio.to_thread``, and
    record_tick telemetry must still fire afterwards."""

    @patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_check_fleet_awaited_directly_and_telemetry_records(self, mock_sleep):
        store = make_store()
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = _watcher(store=store, consumer=consumer)

        with patch.object(
            watcher, "check_fleet", wraps=watcher.check_fleet,
        ) as mock_check_fleet:
            await watcher.run()

        mock_check_fleet.assert_called_once_with()
        events = store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)
