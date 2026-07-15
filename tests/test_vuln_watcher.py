"""Tests for the vulnerability watcher — previously untested. Added
alongside Phase 3 of docs/postgres-migration-plan.md §9, which converted
``run()`` to ``async def`` (``time.sleep()`` -> ``await asyncio.sleep()``).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from agentit.watchers.vuln_watcher import _HEARTBEAT_REFRESH_SECONDS, VulnWatcher
from conftest import make_async_store


def _watcher(store=None, consumer=None) -> VulnWatcher:
    async_store, _raw = store or make_async_store()
    return VulnWatcher(
        publisher=MagicMock(),
        store=async_store,
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
    record_tick telemetry must still fire afterwards. ``self._store`` is
    now the async store directly (no more `.raw`/`AsyncSQLiteStore.wrap`
    bridge inside `check_fleet` itself)."""

    @patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_check_fleet_awaited_directly_and_telemetry_records(self, mock_sleep):
        async_store, raw_store = make_async_store()
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = _watcher(store=(async_store, raw_store), consumer=consumer)

        with patch.object(
            watcher, "check_fleet", wraps=watcher.check_fleet,
        ) as mock_check_fleet:
            await watcher.run()

        mock_check_fleet.assert_called_once_with()
        events = raw_store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)


class TestHeartbeatRefreshedDuringLongSleep:
    """Regression test for the liveness-probe crash loop: the chart's
    livenessProbe (chart/templates/agents/vuln-watcher.yaml) kills the
    container if /tmp/heartbeat is older than 900s, but this watcher's own
    ``--interval`` defaults to 21600s (6h). Touching the heartbeat only once
    per tick means every successful (or failed) tick is followed by a
    guaranteed SIGKILL ~15-19 minutes into the sleep, forever -- confirmed
    live via `oc describe pod` (24 restarts, all "failed liveness probe")
    and postgres tick-complete timestamps exactly 20 minutes apart. The
    fix: refresh the heartbeat periodically during the sleep, not just
    before/after it.
    """

    async def test_sleep_touches_heartbeat_multiple_times_for_long_interval(self):
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = _watcher(consumer=consumer)
        watcher._interval = _HEARTBEAT_REFRESH_SECONDS * 2 + 100

        sleep_calls: list[int] = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        touch_count = 0

        def fake_touch(self):
            nonlocal touch_count
            touch_count += 1

        with patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=fake_sleep), \
             patch("agentit.watchers.vuln_watcher.Path.touch", fake_touch):
            await watcher._sleep_with_heartbeat(watcher._interval)

        # Chunked into 300s + 300s + 100s, heartbeat touched after each chunk.
        assert sleep_calls == [_HEARTBEAT_REFRESH_SECONDS, _HEARTBEAT_REFRESH_SECONDS, 100]
        assert touch_count == 3

    @patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_short_interval_still_stops_on_interrupt(self, mock_sleep):
        """Existing single-chunk behavior (interval < refresh window) is
        preserved -- this is the same scenario `test_run_ticks_once_then_
        stops_on_interrupt` covers end-to-end via run()."""
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = _watcher(consumer=consumer)
        watcher._interval = 1

        try:
            await watcher._sleep_with_heartbeat(1)
            assert False, "expected KeyboardInterrupt to propagate"
        except KeyboardInterrupt:
            pass

        mock_sleep.assert_called_once_with(1)


class TestConstructionAcceptsAsyncStoreDirectly:
    """The real bug fixed here: cli.py used to hand these watchers
    `store.raw` (a plain sync AssessmentStore) because `check_fleet`
    called `self._store.get_fleet_data()` unawaited. Now the store is
    genuinely awaited, so a store whose methods are `async def` (like the
    real async store the CLI passes in) must work end to end -- proven by
    reusing an async store constructed via `create_store()`'s own facade,
    not a hand-rolled stub."""

    async def test_check_fleet_works_against_create_store_facade(self):
        from agentit.portal.store_factory import create_store

        store = await create_store(":memory:")
        watcher = VulnWatcher(publisher=MagicMock(), store=store, consumer=MagicMock(), interval=1)
        await watcher.check_fleet()  # must not raise AttributeError/TypeError
