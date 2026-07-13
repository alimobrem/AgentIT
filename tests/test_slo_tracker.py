"""Tests for the SLO tracker watcher — regression for the bug where
check_once() never collected fresh metric values, only re-read stale
pre-existing status from SQLite."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.watchers.slo_tracker import SloTracker
from conftest import make_report, make_store


def _tracker(store) -> SloTracker:
    return SloTracker(
        publisher=MagicMock(), store=store, consumer=MagicMock(), interval=1,
    )


class TestCollectsFreshValues:
    @patch("agentit.watchers.slo_tracker.collect_slo")
    def test_check_once_collects_and_updates_slo(self, mock_collect):
        """The core regression: check_once must call the collector and persist
        a fresh current_value/status, not just read whatever was last stored."""
        store = make_store()
        report = make_report(repo_name="app1")
        aid = store.save(report)
        sid = store.save_slo(aid, "availability", 99.9)
        assert store.list_slos(aid)[0]["current_value"] is None

        mock_collect.return_value = 99.99  # healthy availability

        tracker = _tracker(store)
        tracker.check_once()

        mock_collect.assert_called_once_with("availability", "app1")
        slo = store.list_slos(aid)[0]
        assert slo["current_value"] == 99.99
        assert slo["status"] == "met"

    @patch("agentit.watchers.slo_tracker.collect_slo")
    def test_check_once_detects_breach_from_fresh_value(self, mock_collect):
        store = make_store()
        report = make_report(repo_name="app2")
        aid = store.save(report)
        store.save_slo(aid, "error_rate", 0.5)
        mock_collect.return_value = 5.0  # way above target -> breach

        tracker = _tracker(store)
        breached_count = tracker.check_once()

        assert breached_count == 1
        slo = store.list_slos(aid)[0]
        assert slo["status"] == "breached"
        assert slo["current_value"] == 5.0

    @patch("agentit.watchers.slo_tracker.collect_slo")
    def test_uncollectable_metric_leaves_prior_status_and_does_not_crash(self, mock_collect):
        """latency_p99_ms (and any metric with no collector) returns None --
        the tracker must skip it cleanly rather than silently doing nothing
        or crashing."""
        store = make_store()
        report = make_report(repo_name="app3")
        aid = store.save(report)
        sid = store.save_slo(aid, "latency_p99_ms", 200.0)
        store.update_slo(sid, 150.0, "met")  # prior status from a previous run
        mock_collect.return_value = None

        tracker = _tracker(store)
        tracker.check_once()

        slo = store.list_slos(aid)[0]
        assert slo["status"] == "met"  # unchanged, not silently reset
        assert slo["current_value"] == 150.0


class TestBreachDirection:
    """availability is higher-is-better; error_rate/latency are lower-is-better."""

    @patch("agentit.watchers.slo_tracker.collect_slo")
    def test_availability_below_target_is_breached(self, mock_collect):
        store = make_store()
        aid = store.save(make_report(repo_name="app4"))
        store.save_slo(aid, "availability", 99.9)
        mock_collect.return_value = 95.0  # below target -> unhealthy

        tracker = _tracker(store)
        tracker.check_once()

        assert store.list_slos(aid)[0]["status"] == "breached"

    @patch("agentit.watchers.slo_tracker.collect_slo")
    def test_availability_above_target_is_met(self, mock_collect):
        store = make_store()
        aid = store.save(make_report(repo_name="app5"))
        store.save_slo(aid, "availability", 99.9)
        mock_collect.return_value = 99.99  # above target -> healthy

        tracker = _tracker(store)
        tracker.check_once()

        assert store.list_slos(aid)[0]["status"] == "met"


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.slo_tracker.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        tracker = _tracker(make_store())
        await tracker.run()

        captured = capsys.readouterr()
        assert "Starting SLO tracker" in captured.err
        assert "SLO tracker stopped." in captured.err
        mock_sleep.assert_called_once_with(1)
