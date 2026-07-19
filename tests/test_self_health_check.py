"""Tests for the self-health-check watcher (`watchers/self_health_check.py`):
periodic functional checks of AgentIT's own critical infrastructure --
GitHub webhook delivery health, CI pipeline stall detection, maintenance
CronJob success, and cleanup-CronJob effectiveness. See
docs/self-health-check-backlog.md for the design rationale.

Mirrors test_drift_detector.py's structure (this watcher follows the exact
same watcher/tick/heartbeat/record_tick pattern) and test_credential_health.py's
mocking conventions for the GitHub API and Health page rendering.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.kube import KubeError
from agentit.portal.app import app
from agentit.portal.helpers import get_self_health_check_states
from agentit.watchers.self_health_check import (
    CHECK_ACTIONS,
    SelfHealthCheck,
)
from conftest import make_async_store, prime_csrf

_AGENTIT_ARGO_APP = {
    "metadata": {"name": "agentit"},
    "spec": {"source": {"repoURL": "https://github.com/alimobrem/AgentIT.git"}},
}


def _checker(**kwargs) -> SelfHealthCheck:
    return SelfHealthCheck(publisher=MagicMock(), interval=1, **kwargs)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ── _check_webhook_reachability ──────────────────────────────────────────


class TestWebhookReachabilityCheck:
    """Reuses `github_pr.check_webhook_delivery_health()` -- the same live
    check backing the Health page's "Webhook Deliveries" section -- as the
    single source of truth for computing this signal, rather than a
    second, independent implementation. These tests cover this watcher's
    own translation of that function's {ok, status, detail} shape into a
    check result, not check_webhook_delivery_health's own logic (covered
    by tests/test_webhook_delivery_health.py)."""

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_failing_delivery_is_critical(self, mock_list):
        mock_list.return_value = [_AGENTIT_ARGO_APP]
        checker = _checker()

        with patch(
            "agentit.portal.github_pr.check_webhook_delivery_health",
            return_value={"ok": False, "status": "failing", "detail": "Last delivery at ...: HTTP 502 (failed)"},
        ):
            result = await checker._check_webhook_reachability()

        assert result["ok"] is False
        assert result["severity"] == "critical"
        assert result["summary"] == "Last delivery at ...: HTTP 502 (failed)"
        assert result["guidance"]

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_not_registered_is_also_critical(self, mock_list):
        """Matches check_webhook_delivery_health's own ok=False for
        "not_registered" -- same severity as an active-but-failing hook,
        for consistency with the Health page's own red/critical rendering
        of any ok=False result."""
        mock_list.return_value = [_AGENTIT_ARGO_APP]
        checker = _checker()

        with patch(
            "agentit.portal.github_pr.check_webhook_delivery_health",
            return_value={"ok": False, "status": "not_registered", "detail": "No webhook ending in ... is registered"},
        ):
            result = await checker._check_webhook_reachability()

        assert result["ok"] is False
        assert result["severity"] == "critical"

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_delivering_is_healthy(self, mock_list):
        mock_list.return_value = [_AGENTIT_ARGO_APP]
        checker = _checker()

        with patch(
            "agentit.portal.github_pr.check_webhook_delivery_health",
            return_value={"ok": True, "status": "delivering", "detail": "Last delivery at ...: HTTP 200 (OK)"},
        ):
            result = await checker._check_webhook_reachability()

        assert result["ok"] is True
        assert result["severity"] == "info"
        assert result["summary"] == "Last delivery at ...: HTTP 200 (OK)"

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_no_deliveries_yet_is_inconclusive_not_a_failure(self, mock_list):
        mock_list.return_value = [_AGENTIT_ARGO_APP]
        checker = _checker()

        with patch(
            "agentit.portal.github_pr.check_webhook_delivery_health",
            return_value={"ok": None, "status": "no_deliveries", "detail": "registered but has no recorded deliveries yet"},
        ):
            result = await checker._check_webhook_reachability()

        assert result["ok"] is None
        assert result["severity"] == "warning"

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_no_agentit_repo_url_skips_check(self, mock_list):
        """No agentit Argo Application (or Argo unreachable) -- must not
        crash, must report inconclusive."""
        mock_list.return_value = []
        checker = _checker()

        result = await checker._check_webhook_reachability()

        assert result["ok"] is None

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources", side_effect=KubeError("unreachable"))
    async def test_argo_api_failure_does_not_crash(self, mock_list):
        checker = _checker()
        result = await checker._check_webhook_reachability()
        assert result["ok"] is None


# ── _check_ci_pipeline_progress ──────────────────────────────────────────


def _pipelinerun(name: str, status: str, reason: str, start_time: str | None) -> dict:
    return {
        "metadata": {
            "name": name, "creationTimestamp": start_time,
            "labels": {"tekton.dev/pipeline": "agentit-ci"},
        },
        "status": {"conditions": [{"status": status, "reason": reason}], "startTime": start_time or ""},
    }


class TestCiPipelineProgressCheck:
    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_stuck_running_past_threshold_is_critical(self, mock_list):
        started = _iso(datetime.now(timezone.utc) - timedelta(minutes=90))
        mock_list.return_value = [_pipelinerun("run-1", "Unknown", "Running", started)]
        checker = _checker(ci_stall_minutes=60)

        result = await checker._check_ci_pipeline_progress()

        assert result["ok"] is False
        assert result["severity"] == "critical"
        assert "run-1" in result["summary"]
        assert result["guidance"]

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_running_within_threshold_is_healthy(self, mock_list):
        started = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        mock_list.return_value = [_pipelinerun("run-2", "Unknown", "Running", started)]
        checker = _checker(ci_stall_minutes=60)

        result = await checker._check_ci_pipeline_progress()

        assert result["ok"] is True
        assert result["severity"] == "info"

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_completed_pipelinerun_is_healthy(self, mock_list):
        started = _iso(datetime.now(timezone.utc) - timedelta(hours=5))
        mock_list.return_value = [_pipelinerun("run-3", "True", "Succeeded", started)]
        checker = _checker()

        result = await checker._check_ci_pipeline_progress()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_ignores_non_agentit_ci_pipelineruns(self, mock_list):
        other = _pipelinerun("run-4", "Unknown", "Running", _iso(datetime.now(timezone.utc) - timedelta(hours=3)))
        other["metadata"]["labels"] = {"tekton.dev/pipeline": "some-other-pipeline"}
        mock_list.return_value = [other]
        checker = _checker()

        result = await checker._check_ci_pipeline_progress()

        assert result["ok"] is True
        assert "No agentit-ci" in result["summary"]

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources")
    async def test_picks_newest_run_when_several_exist(self, mock_list):
        old_started = _iso(datetime.now(timezone.utc) - timedelta(hours=5))
        new_started = _iso(datetime.now(timezone.utc) - timedelta(minutes=2))
        older = _pipelinerun("run-old", "True", "Succeeded", old_started)
        older["metadata"]["creationTimestamp"] = "2020-01-01T00:00:00Z"
        newer = _pipelinerun("run-new", "Unknown", "Running", new_started)
        newer["metadata"]["creationTimestamp"] = "2030-01-01T00:00:00Z"
        mock_list.return_value = [older, newer]
        checker = _checker(ci_stall_minutes=60)

        result = await checker._check_ci_pipeline_progress()

        assert "run-new" in result["summary"]

    @patch("agentit.watchers.self_health_check.kube.list_custom_resources", side_effect=KubeError("unreachable"))
    async def test_api_failure_is_inconclusive_not_a_failure(self, mock_list):
        checker = _checker()
        result = await checker._check_ci_pipeline_progress()
        assert result["ok"] is None
        assert result["severity"] == "warning"


# ── _check_maintenance_cronjobs ───────────────────────────────────────────


def _cj(name: str, suspended=False, last_schedule=None, last_success=None) -> dict:
    return {
        "name": name, "schedule": "*/10 * * * *", "suspended": suspended,
        "last_schedule_time": last_schedule, "last_successful_time": last_success,
        "active_count": 0,
    }


class TestMaintenanceCronjobsCheck:
    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_all_healthy(self, mock_list):
        now = _iso(datetime.now(timezone.utc))
        mock_list.return_value = [_cj("tekton-cleanup", last_schedule=now, last_success=now)]
        checker = _checker()

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_never_succeeded_after_scheduled_is_failing(self, mock_list):
        now = datetime.now(timezone.utc)
        mock_list.return_value = [_cj("tekton-cleanup", last_schedule=_iso(now), last_success=None)]
        checker = _checker(cronjob_grace_minutes=20)

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is False
        assert result["severity"] == "warning"
        assert "tekton-cleanup" in result["summary"]

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_stale_success_beyond_grace_is_failing(self, mock_list):
        now = datetime.now(timezone.utc)
        stale_success = _iso(now - timedelta(hours=2))
        mock_list.return_value = [_cj("secret-rotation", last_schedule=_iso(now), last_success=stale_success)]
        checker = _checker(cronjob_grace_minutes=20)

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is False
        assert "secret-rotation" in result["summary"]

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_recent_success_within_grace_is_healthy(self, mock_list):
        now = datetime.now(timezone.utc)
        recent_success = _iso(now - timedelta(minutes=5))
        mock_list.return_value = [_cj("tekton-cleanup", last_schedule=_iso(now), last_success=recent_success)]
        checker = _checker(cronjob_grace_minutes=20)

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_suspended_cronjobs_are_never_flagged(self, mock_list):
        mock_list.return_value = [_cj("cost-report", suspended=True, last_schedule=None, last_success=None)]
        checker = _checker()

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_never_scheduled_cronjob_is_not_flagged(self, mock_list):
        """A freshly-installed CronJob that hasn't had its first scheduled
        run yet must not be treated as failing."""
        mock_list.return_value = [_cj("dependency-update", last_schedule=None, last_success=None)]
        checker = _checker()

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs")
    async def test_no_cronjobs_found_is_healthy(self, mock_list):
        mock_list.return_value = []
        checker = _checker()

        result = await checker._check_maintenance_cronjobs()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.list_cronjobs", side_effect=KubeError("unreachable"))
    async def test_api_failure_is_inconclusive(self, mock_list):
        checker = _checker()
        result = await checker._check_maintenance_cronjobs()
        assert result["ok"] is None


# ── _check_cleanup_effectiveness ─────────────────────────────────────────


class TestCleanupEffectivenessCheck:
    @patch("agentit.watchers.self_health_check.kube.count_stale_terminal_pods")
    async def test_backlog_under_threshold_is_healthy(self, mock_count):
        mock_count.return_value = 2
        checker = _checker(stale_pod_count_threshold=10)

        result = await checker._check_cleanup_effectiveness()

        assert result["ok"] is True

    @patch("agentit.watchers.self_health_check.kube.count_stale_terminal_pods")
    async def test_backlog_over_threshold_is_warning(self, mock_count):
        """Even if the CronJob itself reports success, a growing stale-pod
        backlog is the generic 'looks healthy, does nothing' signal from
        the 2026-07-18 tekton-cleanup incident."""
        mock_count.return_value = 25
        checker = _checker(stale_pod_count_threshold=10)

        result = await checker._check_cleanup_effectiveness()

        assert result["ok"] is False
        assert result["severity"] == "warning"
        assert "25" in result["summary"]
        assert result["guidance"]

    @patch(
        "agentit.watchers.self_health_check.kube.count_stale_terminal_pods",
        side_effect=KubeError("unreachable"),
    )
    async def test_api_failure_is_inconclusive(self, mock_count):
        checker = _checker()
        result = await checker._check_cleanup_effectiveness()
        assert result["ok"] is None


# ── check_once / _publish_result: dual-write + telemetry ─────────────────


class TestPublishResultDualWrite:
    """Unlike DriftDetector's Kafka-only gitops-lag-detected, every check
    result here must be persisted to the store too, so it's genuinely
    visible on /api/events and the Health page panel."""

    async def test_publishes_to_kafka_and_persists_to_store(self):
        async_store, raw_store = await make_async_store()
        publisher = MagicMock()
        checker = SelfHealthCheck(publisher=publisher, store=async_store, interval=1)

        result = {
            "ok": True, "action": "self-check-webhook", "severity": "info",
            "summary": "all good", "guidance": None, "details": {},
        }
        await checker._publish_result(result)

        publisher.publish.assert_called_once()
        assert publisher.publish.call_args.kwargs["action"] == "self-check-webhook"

        events = await raw_store.list_events_by_agent("self-health-check")
        assert len(events) == 1
        assert events[0]["action"] == "self-check-webhook"
        assert events[0]["severity"] == "info"

    async def test_failing_check_persists_guidance_in_details(self):
        async_store, raw_store = await make_async_store()
        checker = SelfHealthCheck(publisher=MagicMock(), store=async_store, interval=1)

        result = {
            "ok": False, "action": "self-check-ci-pipeline", "severity": "critical",
            "summary": "stuck", "guidance": "check the pod", "details": {"pipelinerun": "run-1"},
        }
        await checker._publish_result(result)

        events = await raw_store.list_events_by_agent("self-health-check")
        import json
        details = json.loads(events[0]["details_json"])
        assert details["guidance"] == "check the pod"
        assert details["pipelinerun"] == "run-1"

    async def test_store_failure_does_not_raise(self):
        publisher = MagicMock()
        broken_store = MagicMock()
        broken_store.log_event = MagicMock(side_effect=RuntimeError("db down"))
        checker = SelfHealthCheck(publisher=publisher, store=broken_store, interval=1)

        result = {
            "ok": True, "action": "self-check-webhook", "severity": "info",
            "summary": "ok", "guidance": None, "details": {},
        }
        await checker._publish_result(result)  # must not raise

    async def test_no_store_only_publishes_to_kafka(self):
        publisher = MagicMock()
        checker = SelfHealthCheck(publisher=publisher, store=None, interval=1)

        result = {
            "ok": True, "action": "self-check-webhook", "severity": "info",
            "summary": "ok", "guidance": None, "details": {},
        }
        await checker._publish_result(result)  # must not raise

        publisher.publish.assert_called_once()


class TestCheckOnceRunsAllFourChecks:
    @patch("agentit.watchers.self_health_check.kube.count_stale_terminal_pods", return_value=0)
    @patch("agentit.watchers.self_health_check.kube.list_cronjobs", return_value=[])
    @patch("agentit.watchers.self_health_check.kube.list_custom_resources", return_value=[])
    async def test_runs_and_publishes_all_four_checks_even_if_independent(
        self, mock_argo, mock_cronjobs, mock_pods,
    ):
        publisher = MagicMock()
        checker = SelfHealthCheck(publisher=publisher, interval=1)

        results = await checker.check_once()

        assert len(results) == 4
        assert {r["action"] for r in results} == set(CHECK_ACTIONS)
        assert publisher.publish.call_count == 4


class TestTickTelemetry:
    async def test_run_ticks_once_records_success_and_stops_on_interrupt(self):
        async_store, raw_store = await make_async_store()
        checker = SelfHealthCheck(publisher=MagicMock(), store=async_store, interval=1)

        with patch(
            "agentit.watchers.self_health_check.kube.list_custom_resources", return_value=[],
        ), patch(
            "agentit.watchers.self_health_check.kube.list_cronjobs", return_value=[],
        ), patch(
            "agentit.watchers.self_health_check.kube.count_stale_terminal_pods", return_value=0,
        ), patch(
            "agentit.watchers.self_health_check.sleep_with_heartbeat", side_effect=KeyboardInterrupt,
        ):
            await checker.run()

        events = await raw_store.list_events_by_agent("self-health-check")
        assert any(e["action"] == "tick-complete" for e in events)

    async def test_tick_failure_is_recorded_not_raised(self):
        async_store, raw_store = await make_async_store()
        checker = SelfHealthCheck(publisher=MagicMock(), store=async_store, interval=1)

        with patch(
            "agentit.watchers.self_health_check.kube.list_custom_resources",
            side_effect=RuntimeError("boom"),
        ), patch(
            "agentit.watchers.self_health_check.sleep_with_heartbeat", side_effect=KeyboardInterrupt,
        ):
            await checker.run()  # must not raise

        events = await raw_store.list_events_by_agent("self-health-check")
        assert any(e["action"] == "tick-failed" for e in events)


# ── Health page: "AgentIT Self-Health" panel ──────────────────────────────


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


class TestGetSelfHealthCheckStates:
    async def test_no_events_reports_unknown_for_every_check(self):
        async_store, _raw = await make_async_store()
        states = await get_self_health_check_states(async_store)

        assert set(states.keys()) == set(CHECK_ACTIONS)
        for action in CHECK_ACTIONS:
            assert states[action]["status"] == "unknown"
            assert states[action]["ok"] is None

    async def test_reads_back_most_recent_result_per_check(self):
        async_store, raw_store = await make_async_store()
        await raw_store.log_event(
            "self-health-check", "self-check-webhook", "agentit", "critical",
            "all deliveries failing", details={"guidance": "check oauth-proxy config"},
        )

        states = await get_self_health_check_states(async_store)

        assert states["self-check-webhook"]["status"] == "critical"
        assert states["self-check-webhook"]["ok"] is False
        assert states["self-check-webhook"]["guidance"] == "check oauth-proxy config"
        assert states["self-check-webhook"]["summary"] == "all deliveries failing"

    async def test_only_the_newest_event_per_action_wins(self):
        async_store, raw_store = await make_async_store()
        await raw_store.log_event(
            "self-health-check", "self-check-webhook", "agentit", "critical", "old failure",
        )
        await raw_store.log_event(
            "self-health-check", "self-check-webhook", "agentit", "info", "now healthy",
        )

        states = await get_self_health_check_states(async_store)

        assert states["self-check-webhook"]["status"] == "healthy"
        assert states["self-check-webhook"]["summary"] == "now healthy"

    async def test_store_failure_reports_unknown_not_a_crash(self):
        broken_store = MagicMock()

        async def _raise(*args, **kwargs):
            raise RuntimeError("db down")
        broken_store.list_events_by_agent = _raise

        states = await get_self_health_check_states(broken_store)

        assert all(s["status"] == "unknown" for s in states.values())


def _row(html: str, needle: str) -> str:
    rows = html.split("<tr")
    return next(r for r in rows if needle in r)


class TestSelfHealthPanelRendering:
    async def test_health_page_shows_panel_with_all_checks(self, client):
        with patch("agentit.portal.routes.health.kube") as mock_kube:
            mock_kube.list_custom_resources.return_value = []
            resp = await client.get("/health")

        assert resp.status_code == 200
        assert "AgentIT Self-Health" in resp.text
        assert "GitHub webhook reachability" in resp.text
        assert "CI pipeline progress" in resp.text
        assert "Maintenance CronJob success" in resp.text
        assert "Cleanup effectiveness" in resp.text

    async def test_healthy_check_renders_green(self, client):
        # Writes through the shared test-session store (same real Postgres
        # instance `routes.health.get_store()`'s own singleton connects
        # to) -- no need to patch get_store itself, matching
        # test_credential_health.py's convention.
        _async_store, raw_store = await make_async_store()
        await raw_store.log_event(
            "self-health-check", "self-check-webhook", "agentit", "info", "webhook healthy",
        )
        with patch("agentit.portal.routes.health.kube") as mock_kube:
            mock_kube.list_custom_resources.return_value = []
            resp = await client.get("/health")

        row = _row(resp.text, "GitHub webhook reachability")
        assert "row-border-green" in row
        assert "badge-low" in row

    async def test_critical_check_renders_red_with_guidance(self, client):
        _async_store, raw_store = await make_async_store()
        await raw_store.log_event(
            "self-health-check", "self-check-ci-pipeline", "agentit", "critical",
            "pipeline stuck for 90 minutes", details={"guidance": "check pod scheduling"},
        )
        with patch("agentit.portal.routes.health.kube") as mock_kube:
            mock_kube.list_custom_resources.return_value = []
            resp = await client.get("/health")

        row = _row(resp.text, "CI pipeline progress")
        assert "row-border-red" in row
        assert "badge-critical" in row
        assert "check pod scheduling" in row
