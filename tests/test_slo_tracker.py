"""Tests for the SLO tracker watcher — regression for the bug where
check_once() never collected fresh metric values, only re-read stale
pre-existing status from SQLite."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from agentit.watchers.slo_tracker import SloTracker
from conftest import make_async_store, make_report


def _tracker(store) -> SloTracker:
    return SloTracker(
        publisher=MagicMock(), store=store, consumer=MagicMock(), interval=1,
    )


class TestCollectsFreshValues:
    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_check_once_collects_and_updates_slo(self, mock_collect):
        """The core regression: check_once must call the collector and persist
        a fresh current_value/status, not just read whatever was last stored."""
        async_store, store = await make_async_store()
        report = make_report(repo_name="app1")
        aid = await store.save(report)
        sid = await store.save_slo(aid, "availability", 99.9)
        assert (await store.list_slos(aid))[0]["current_value"] is None

        mock_collect.return_value = 99.99  # healthy availability

        tracker = _tracker(async_store)
        await tracker.check_once()

        mock_collect.assert_called_once_with("availability", "app1")
        slo = (await store.list_slos(aid))[0]
        assert slo["current_value"] == 99.99
        assert slo["status"] == "met"

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_check_once_detects_breach_from_fresh_value(self, mock_collect):
        async_store, store = await make_async_store()
        report = make_report(repo_name="app2")
        aid = await store.save(report)
        await store.save_slo(aid, "error_rate", 0.5)
        mock_collect.return_value = 5.0  # way above target -> breach

        tracker = _tracker(async_store)
        breached_count = await tracker.check_once()

        assert breached_count == 1
        slo = (await store.list_slos(aid))[0]
        assert slo["status"] == "breached"
        assert slo["current_value"] == 5.0

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_uncollectable_metric_leaves_prior_status_and_does_not_crash(self, mock_collect):
        """latency_p99_ms (and any metric with no collector) returns None --
        the tracker must skip it cleanly rather than silently doing nothing
        or crashing."""
        async_store, store = await make_async_store()
        report = make_report(repo_name="app3")
        aid = await store.save(report)
        sid = await store.save_slo(aid, "latency_p99_ms", 200.0)
        await store.update_slo(sid, 150.0, "met")  # prior status from a previous run
        mock_collect.return_value = None

        tracker = _tracker(async_store)
        await tracker.check_once()

        slo = (await store.list_slos(aid))[0]
        assert slo["status"] == "met"  # unchanged, not silently reset
        assert slo["current_value"] == 150.0


class TestBreachDirection:
    """availability is higher-is-better; error_rate/latency are lower-is-better."""

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_availability_below_target_is_breached(self, mock_collect):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app4"))
        await store.save_slo(aid, "availability", 99.9)
        mock_collect.return_value = 95.0  # below target -> unhealthy

        tracker = _tracker(async_store)
        await tracker.check_once()

        assert (await store.list_slos(aid))[0]["status"] == "breached"

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_availability_above_target_is_met(self, mock_collect):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app5"))
        await store.save_slo(aid, "availability", 99.9)
        mock_collect.return_value = 99.99  # above target -> healthy

        tracker = _tracker(async_store)
        await tracker.check_once()

        assert (await store.list_slos(aid))[0]["status"] == "met"


class TestOneTickPerApp:
    """list_all() returns every historical assessment; the tracker must tick
    each repo_url once so re-assessed apps do not get N rollback-review gates."""

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_check_once_dedupes_assessments_of_same_app(self, mock_collect):
        async_store, store = await make_async_store()
        old_id = await store.save(make_report(repo_name="app-triple"))
        await store.save_slo(old_id, "error_rate", 0.5)
        await store.save_apply_results(
            old_id, {"applied": ["deployment.yaml"], "skipped": [], "errors": []},
            "app-triple", dry_run=False,
        )
        new_id = await store.save(make_report(repo_name="app-triple"))
        assert new_id != old_id
        await store.save_apply_results(
            new_id, {"applied": ["deployment.yaml"], "skipped": [], "errors": []},
            "app-triple", dry_run=False,
        )
        # list_slos joins by repo_url, so either assessment sees the same SLO.
        # Without per-app uniquify, collect_slo would run once per assessment.
        mock_collect.return_value = 5.0  # breach

        tracker = _tracker(async_store)
        breached_count = await tracker.check_once()

        assert breached_count == 1
        mock_collect.assert_called_once_with("error_rate", "app-triple")
        gates = await store.list_gates_for_assessment(new_id, status="pending")
        rollback = [g for g in gates if g["gate_type"] == "rollback-review"]
        assert len(rollback) == 1


class TestRollbackRecommendationLogged:
    """docs/ledger-design-spec.md Phase 0: a rollback recommendation must be
    persisted via log_event(), not only published to Kafka's TOPIC_ALERTS --
    otherwise it's invisible on the app's own timeline (Ledger card type J)."""

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_recommend_rollback_logs_event_alongside_kafka_publish(self, mock_collect):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app-rollback"))
        await store.save_slo(aid, "error_rate", 0.5)
        await store.save_apply_results(
            aid, {"applied": ["deployment.yaml"], "skipped": [], "errors": []},
            "app-rollback", dry_run=False,
        )
        mock_collect.return_value = 5.0  # breach

        tracker = _tracker(async_store)
        await tracker.check_once()

        events = await store.list_events()
        rollback_events = [e for e in events if e["action"] == "rollback-recommended"]
        assert len(rollback_events) == 1
        assert rollback_events[0]["target_app"] == "app-rollback"
        assert rollback_events[0]["severity"] == "critical"
        assert "rollback" in rollback_events[0]["summary"].lower()

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_no_rollback_event_without_a_prior_apply(self, mock_collect):
        """_recommend_rollback returns early when there's no applied result --
        no event, no gate, matching the pre-existing early-return behavior."""
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app-no-apply"))
        await store.save_slo(aid, "error_rate", 0.5)
        mock_collect.return_value = 5.0

        tracker = _tracker(async_store)
        await tracker.check_once()

        events = await store.list_events()
        assert not any(e["action"] == "rollback-recommended" for e in events)


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.slo_tracker.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        async_store, _store = await make_async_store()
        tracker = _tracker(async_store)
        await tracker.run()

        captured = capsys.readouterr()
        assert "Starting SLO tracker" in captured.err
        assert "SLO tracker stopped." in captured.err
        mock_sleep.assert_called_once_with(1)


class TestTickRunsOnEventLoop:
    """``check_once`` is now a genuine coroutine -- ``run()`` awaits it
    directly rather than dispatching the whole tick to a worker thread.
    The one truly blocking call inside it (``collect_slo``) is still
    narrowly wrapped in ``asyncio.to_thread`` in ``_collect_fresh_values``,
    and record_tick telemetry must still fire afterwards."""

    @patch("agentit.watchers.slo_tracker.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_check_once_awaited_directly_and_telemetry_records(self, mock_sleep):
        async_store, store = await make_async_store()
        tracker = _tracker(async_store)

        with patch.object(tracker, "check_once", wraps=tracker.check_once) as mock_check_once:
            await tracker.run()

        mock_check_once.assert_called_once_with()
        events = await store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)

    @patch("agentit.watchers.slo_tracker.collect_slo")
    async def test_collect_slo_dispatched_via_to_thread(self, mock_collect):
        """The narrow-to_thread call site: collect_slo (blocking kube I/O)
        must not run directly on the event loop."""
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app6"))
        await store.save_slo(aid, "availability", 99.9)
        mock_collect.return_value = 99.99

        tracker = _tracker(async_store)
        with patch(
            "agentit.watchers.slo_tracker.asyncio.to_thread", wraps=asyncio.to_thread
        ) as mock_to_thread:
            await tracker.check_once()

        mock_to_thread.assert_any_call(mock_collect, "availability", "app6")


class TestConstructionAcceptsAsyncStoreDirectly:
    """The real bug fixed here: cli.py used to hand SloTracker `store.raw`
    because `check_once` called every store method unawaited. Now the
    store is genuinely awaited throughout, so a store constructed via
    `create_store()`'s own facade must work end to end."""

    async def test_check_once_works_against_create_store_facade(self, postgres_dsn):
        from agentit.portal.store import create_store

        store = await create_store(postgres_dsn, min_size=1, max_size=2)
        tracker = SloTracker(publisher=MagicMock(), store=store, consumer=MagicMock(), interval=1)
        breached = await tracker.check_once()  # must not raise AttributeError/TypeError
        assert breached == 0
