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


def test_check_fleet_with_empty_fleet_is_a_noop():
    watcher = _watcher()
    watcher.check_fleet()  # must not raise, even with no tracked apps


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


class TestTickRunsOffEventLoop:
    """check_fleet must be dispatched via asyncio.to_thread so it doesn't
    block the event loop for the tick's full duration, and record_tick
    telemetry must still fire afterwards."""

    @patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_check_fleet_dispatched_via_to_thread_and_telemetry_records(self, mock_sleep):
        store = make_store()
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = _watcher(store=store, consumer=consumer)

        with patch(
            "agentit.watchers.vuln_watcher.asyncio.to_thread", wraps=asyncio.to_thread
        ) as mock_to_thread:
            await watcher.run()

        mock_to_thread.assert_called_once_with(watcher.check_fleet)
        events = store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)
