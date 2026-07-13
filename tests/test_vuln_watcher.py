"""Tests for the vulnerability watcher — previously untested. Added
alongside Phase 3 of docs/postgres-migration-plan.md §9, which converted
``run()`` to ``async def`` (``time.sleep()`` -> ``await asyncio.sleep()``).
"""
from __future__ import annotations

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
