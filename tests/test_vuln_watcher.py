"""Tests for the vulnerability watcher — previously untested. Added
alongside Phase 3 of docs/postgres-migration-plan.md §9, which converted
``run()`` to ``async def`` (``time.sleep()`` -> ``await asyncio.sleep()``).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.models import DimensionScore, Finding, Severity
from agentit.watchers.vuln_watcher import VulnWatcher
from conftest import make_async_store, make_report


async def _watcher(store=None, consumer=None, publisher=None) -> VulnWatcher:
    async_store, _raw = store or await make_async_store()
    return VulnWatcher(
        publisher=publisher or MagicMock(),
        store=async_store,
        consumer=consumer or MagicMock(),
        interval=1,
    )


def _report_with_critical_finding(**kwargs):
    report = make_report(**kwargs)
    report.scores = [
        DimensionScore(
            dimension="security", score=20, max_score=100,
            findings=[Finding(category="container", severity=Severity.critical,
                               description="Root user in Containerfile", recommendation="Add USER directive")],
        ),
    ]
    report.overall_score = 20
    return report


async def test_check_fleet_with_empty_fleet_is_a_noop():
    watcher = await _watcher()
    await watcher.check_fleet()  # must not raise, even with no tracked apps


class TestCriticalFindingsSurfaceAsAlertsOnly:
    """AutoMode (and the RemediationLoop pipeline it used to trigger here for
    critical/high findings -- both since fully deleted) has been removed:
    check_fleet() now only ever publishes an alert -- fixing a finding
    always requires a human to explicitly Assess/Onboard/Deliver for that
    app, never an autonomous background trigger."""

    async def test_critical_findings_publish_alert_and_never_trigger_remediation(self):
        async_store, raw_store = await make_async_store()
        await raw_store.save(_report_with_critical_finding(repo_name="critical-app"))

        publisher = MagicMock()
        watcher = await _watcher(store=(async_store, raw_store), publisher=publisher)

        await watcher.check_fleet()

        alert_calls = [
            c for c in publisher.publish.call_args_list
            if c.kwargs.get("action") == "critical-findings-detected"
        ]
        assert len(alert_calls) == 1
        assert alert_calls[0].kwargs["target_app"] == "critical-app"
        assert alert_calls[0].kwargs["severity"] == "warning"


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.vuln_watcher.sleep_with_heartbeat", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = await _watcher(consumer=consumer)
        await watcher.run()

        captured = capsys.readouterr()
        assert "Starting vulnerability watcher" in captured.err
        assert "Vulnerability watcher stopped." in captured.err
        mock_sleep.assert_called_once_with(1)


class TestTickRunsOnEventLoop:
    """``check_fleet`` is a genuine coroutine -- ``run()`` `await`s it
    directly rather than dispatching the whole tick to a worker thread via
    ``asyncio.to_thread``, and record_tick telemetry must still fire
    afterwards. ``self._store`` is the async store directly (no more
    `.raw`/`AsyncSQLiteStore.wrap` bridge inside `check_fleet` itself)."""

    @patch("agentit.watchers.vuln_watcher.sleep_with_heartbeat", side_effect=KeyboardInterrupt)
    async def test_check_fleet_awaited_directly_and_telemetry_records(self, mock_sleep):
        async_store, raw_store = await make_async_store()
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = await _watcher(store=(async_store, raw_store), consumer=consumer)

        with patch.object(
            watcher, "check_fleet", wraps=watcher.check_fleet,
        ) as mock_check_fleet:
            await watcher.run()

        mock_check_fleet.assert_called_once_with()
        events = await raw_store.list_events()
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
    before/after it -- now implemented once, shared, in
    ``agentit.watchers.sleep_with_heartbeat`` (see test_watchers_init.py for
    the chunking behavior itself); this class only confirms ``run()``
    actually delegates its between-tick sleep to that shared helper with
    the watcher's own interval, instead of a bare ``asyncio.sleep``.
    """

    async def test_run_delegates_between_tick_sleep_to_shared_heartbeat_helper(self):
        consumer = MagicMock()
        consumer.poll_once.return_value = []
        watcher = await _watcher(consumer=consumer)
        watcher._interval = 12345

        with patch(
            "agentit.watchers.vuln_watcher.sleep_with_heartbeat", side_effect=KeyboardInterrupt,
        ) as mock_sleep:
            await watcher.run()

        mock_sleep.assert_called_once_with(12345)


class TestConstructionAcceptsAsyncStoreDirectly:
    """The real bug fixed here: cli.py used to hand these watchers
    `store.raw` (a plain sync AssessmentStore) because `check_fleet`
    called `self._store.get_fleet_data()` unawaited. Now the store is
    genuinely awaited, so a store whose methods are `async def` (like the
    real async store the CLI passes in) must work end to end -- proven by
    reusing an async store constructed via `create_store()`'s own facade,
    not a hand-rolled stub."""

    async def test_check_fleet_works_against_create_store_facade(self, postgres_dsn):
        from agentit.portal.store import create_store

        store = await create_store(postgres_dsn, min_size=1, max_size=2)
        watcher = VulnWatcher(publisher=MagicMock(), store=store, consumer=MagicMock(), interval=1)
        await watcher.check_fleet()  # must not raise AttributeError/TypeError
