"""Tests for the re-assessment scheduler watcher
(watchers/reassess_scheduler.py) -- the mechanism that automatically
re-Assesses apps once their configured cadence (apps.assessment_cadence)
has elapsed, following the exact same long-lived-watcher pattern
(``run()``/``sleep_with_heartbeat``/``record_tick``) as
vuln_watcher.py/drift_detector.py, and calling back into the portal via
``internal_webhook_client`` exactly like RemediationLoop/SkillLearner
already do.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from agentit.watchers.reassess_scheduler import ReassessScheduler
from conftest import make_async_store, make_report


def _scheduler(store=None, portal_url: str = "http://bad-host:9999") -> ReassessScheduler:
    return ReassessScheduler(store=store, interval=1, portal_url=portal_url, timeout=2)


class TestCheckDueApps:
    async def test_noop_with_no_due_apps(self):
        store, _raw = await make_async_store()
        scheduler = _scheduler(store=store)

        results = await scheduler.check_due_apps()  # must not raise, even with no tracked apps

        assert results == []
        await scheduler.close()

    async def test_triggers_assess_for_a_due_app_via_the_shared_webhook_route(self):
        """The real mechanism: calling POST /api/webhook/assess -- the
        exact same route RemediationLoop/Tekton/the manual Fleet Re-assess
        button already use -- not a second, parallel assess code path."""
        store, raw = await make_async_store()
        report = make_report(repo_name="due-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        await raw.save(report)

        scheduler = _scheduler(store=store)
        scheduler._client.post = AsyncMock(
            return_value=httpx.Response(200, json={"assessment_id": "abc123", "overall_score": 88})
        )

        results = await scheduler.check_due_apps()

        assert len(results) == 1
        assert results[0]["repo_name"] == "due-app"
        scheduler._client.post.assert_called_once_with(
            "http://bad-host:9999/api/webhook/assess",
            json={"repo_url": report.repo_url, "criticality": "medium"},
        )
        await scheduler.close()

    async def test_logs_auto_reassess_triggered_event_on_success(self):
        store, raw = await make_async_store()
        report = make_report(repo_name="due-app-logged")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        await raw.save(report)

        scheduler = _scheduler(store=store)
        scheduler._client.post = AsyncMock(
            return_value=httpx.Response(200, json={"assessment_id": "abc123", "overall_score": 88})
        )
        await scheduler.check_due_apps()

        events = await raw.list_events()
        triggered = [e for e in events if e["action"] == "auto-reassess-triggered"]
        assert len(triggered) == 1
        assert triggered[0]["target_app"] == "due-app-logged"
        assert triggered[0]["severity"] == "info"
        await scheduler.close()

    async def test_logs_auto_reassess_failed_event_on_http_error(self):
        store, raw = await make_async_store()
        report = make_report(repo_name="due-app-failing")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        await raw.save(report)

        scheduler = _scheduler(store=store)
        scheduler._client.post = AsyncMock(
            return_value=httpx.Response(500, text="internal error")
        )
        await scheduler.check_due_apps()

        events = await raw.list_events()
        failed = [e for e in events if e["action"] == "auto-reassess-failed"]
        assert len(failed) == 1
        assert failed[0]["target_app"] == "due-app-failing"
        assert failed[0]["severity"] == "warning"
        await scheduler.close()

    async def test_handles_duplicate_response_without_raising_or_logging_failure(self):
        """/api/webhook/assess's own dedup guard (claim_webhook) can return
        {"status": "duplicate", ...} instead of a real assessment result --
        this must not be treated as an error (see RemediationLoop._assess's
        identical handling of the same shape)."""
        store, raw = await make_async_store()
        report = make_report(repo_name="due-app-duplicate")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        await raw.save(report)

        scheduler = _scheduler(store=store)
        scheduler._client.post = AsyncMock(
            return_value=httpx.Response(200, json={"status": "duplicate", "delivery_id": "xyz"})
        )
        await scheduler.check_due_apps()  # must not raise

        events = await raw.list_events()
        assert not any(e["action"] == "auto-reassess-failed" for e in events)
        await scheduler.close()

    async def test_never_triggers_assess_for_a_manual_cadence_app(self):
        store, raw = await make_async_store()
        report = make_report(repo_name="manual-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=400)
        await raw.save(report)
        await raw.set_assessment_cadence(report.repo_url, "manual")

        scheduler = _scheduler(store=store)
        scheduler._client.post = AsyncMock()

        results = await scheduler.check_due_apps()

        assert results == []
        scheduler._client.post.assert_not_called()
        await scheduler.close()


class TestPortalUrlConfiguration:
    def test_defaults_to_env_var(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_PORTAL_URL", "http://agentit.agentit.svc:8080")
        scheduler = ReassessScheduler(store=None, interval=1)
        assert scheduler._portal == "http://agentit.agentit.svc:8080"

    def test_explicit_portal_url_overrides_env(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_PORTAL_URL", "http://should-not-be-used:8080")
        scheduler = ReassessScheduler(store=None, interval=1, portal_url="http://explicit:9090")
        assert scheduler._portal == "http://explicit:9090"


class TestAsyncRunLoop:
    """Same tick-once-then-stop-on-KeyboardInterrupt convention every other
    watcher's run() test uses (vuln_watcher/drift_detector)."""

    @patch("agentit.watchers.reassess_scheduler.sleep_with_heartbeat", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        store, _raw = await make_async_store()
        scheduler = _scheduler(store=store)

        await scheduler.run()

        captured = capsys.readouterr()
        assert "Starting re-assessment scheduler" in captured.err
        assert "Re-assessment scheduler stopped." in captured.err
        mock_sleep.assert_called_once_with(1)
        await scheduler.close()

    @patch("agentit.watchers.reassess_scheduler.sleep_with_heartbeat", side_effect=KeyboardInterrupt)
    async def test_run_records_tick_complete_telemetry(self, mock_sleep):
        store, raw = await make_async_store()
        scheduler = _scheduler(store=store)

        await scheduler.run()

        events = await raw.list_events()
        assert any(e["action"] == "tick-complete" for e in events)
        await scheduler.close()

    @patch("agentit.watchers.reassess_scheduler.sleep_with_heartbeat", side_effect=KeyboardInterrupt)
    async def test_run_records_tick_failed_telemetry_on_exception(self, mock_sleep):
        store, raw = await make_async_store()
        scheduler = _scheduler(store=store)
        scheduler.check_due_apps = AsyncMock(side_effect=RuntimeError("boom"))

        await scheduler.run()  # must not raise -- caught, logged, then sleeps (and stops on the interrupt)

        events = await raw.list_events()
        failed = [e for e in events if e["action"] == "tick-failed"]
        assert len(failed) == 1
        assert "boom" in failed[0]["summary"]
        await scheduler.close()


class TestClose:
    async def test_close_closes_the_underlying_http_client(self):
        scheduler = _scheduler(store=None)
        scheduler._client.aclose = AsyncMock()
        await scheduler.close()
        scheduler._client.aclose.assert_called_once()
