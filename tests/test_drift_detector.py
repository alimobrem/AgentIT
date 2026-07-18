"""Tests for the drift detector watcher — regression for the AttributeError
crash from referencing DriftResult.has_warnings / DriftResult.deprecated_apis,
neither of which exist on the real dataclass."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from agentit.api_drift_detector import DriftResult
from agentit.kube import KubeError
from agentit.platform_context import PlatformContext
from agentit.watchers.drift_detector import DriftDetector
from conftest import make_async_store


def _detector() -> DriftDetector:
    return DriftDetector(publisher=MagicMock(), interval=1)


_SYNCED_ARGO_APP = {
    "metadata": {"name": "some-app"},
    "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
}


class TestFetchArgoAppsNamespace:
    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    def test_fetch_argo_apps_scopes_to_openshift_gitops_namespace(self, mock_list):
        """Regression: live bug where this call omitted namespace= entirely,
        which makes the kubernetes client issue a cluster-scoped list --
        403s even for an SA correctly granted the namespace-scoped
        `-argocd-read` Role rbac.yaml binds only in openshift-gitops (the
        same namespace every other Argo Application lookup in this repo
        already scopes to: health.py, fleet.py, delivery.py)."""
        mock_list.return_value = [_SYNCED_ARGO_APP]
        detector = _detector()

        detector._fetch_argo_apps()

        mock_list.assert_called_once_with(
            "argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops",
        )


class TestApiDriftWarnings:
    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_detect_once_does_not_crash_with_deprecated_apis(self, mock_list):
        """Regression: previously raised AttributeError every tick because
        DriftResult has no has_warnings/deprecated_apis attributes. Requires
        Argo CD access to be reachable so the code actually gets to the API
        drift detection block (otherwise detect_once returns early)."""
        mock_list.return_value = [_SYNCED_ARGO_APP]
        detector = _detector()

        ctx = PlatformContext(
            k8s_version="1.25",
            available_kinds={"deployments"},
            deprecated_apis=[{"api": "policy/v1beta1 PodSecurityPolicy", "removed_in": "1.25"}],
        )
        with patch("agentit.platform_context.discover_platform", return_value=ctx), \
             patch("agentit.api_drift_detector.detect_drift", return_value=DriftResult()):
            # Must not raise.
            result = await detector.detect_once()

        assert result == []

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_detect_once_reports_deprecated_apis_from_platform_context(self, mock_list, capsys):
        """ctx.deprecated_apis (the real field) should drive the WARNING
        message, not the nonexistent api_drift.deprecated_apis."""
        mock_list.return_value = [_SYNCED_ARGO_APP]
        detector = _detector()

        ctx = PlatformContext(
            k8s_version="1.25",
            available_kinds={"deployments"},
            deprecated_apis=[
                {"api": "policy/v1beta1 PodSecurityPolicy", "removed_in": "1.25"},
                {"api": "autoscaling/v2beta1 HorizontalPodAutoscaler", "removed_in": "1.26"},
            ],
        )
        with patch("agentit.platform_context.discover_platform", return_value=ctx), \
             patch("agentit.api_drift_detector.detect_drift", return_value=DriftResult()):
            await detector.detect_once()

        captured = capsys.readouterr()
        assert "2 deprecated API(s)" in captured.err

    def test_drift_result_has_no_has_warnings_field(self):
        """Documents the real DriftResult shape so this doesn't regress silently."""
        result = DriftResult()
        assert not hasattr(result, "has_warnings")
        assert not hasattr(result, "deprecated_apis")


class TestDriftDetectorTickTelemetry:
    async def test_accepts_optional_store_for_tick_telemetry(self):
        async_store, _raw = await make_async_store()
        detector = DriftDetector(publisher=MagicMock(), interval=1, store=async_store)
        assert detector._store is async_store

    def test_defaults_to_none_store_when_omitted(self):
        detector = _detector()
        assert detector._store is None

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_maybe_auto_sync_reuses_injected_store(self, mock_list):
        """_maybe_auto_sync previously always created a brand-new AssessmentStore()
        even when the detector already had one -- it should reuse the injected
        store when present. ``self._store`` is now the async store directly,
        no more `.raw`/`AsyncSQLiteStore.wrap` bridge inside `_maybe_auto_sync`."""
        async_store, raw_store = await make_async_store()
        await raw_store.set_setting("auto_mode", "false")
        detector = DriftDetector(publisher=MagicMock(), interval=1, store=async_store)
        await detector._maybe_auto_sync("some-app")  # auto-mode off -> returns early, no crash


_AGENTIT_APP = {
    "metadata": {"name": "agentit"},
    "spec": {"source": {"repoURL": "https://github.com/alimobrem/AgentIT.git"}},
    "status": {
        "sync": {"status": "Synced", "revision": "a" * 40},
        "health": {"status": "Healthy"},
    },
}


class TestGitopsLagDetection:
    """2026-07-17 incident: notify-argocd got stuck for hours with nothing
    telling anyone commits on main had stopped reaching the cluster. See
    docs/gitops-lag-alerting.md for the full design writeup."""

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_publishes_critical_event_when_far_behind_main(self, mock_list):
        mock_list.return_value = [_AGENTIT_APP]
        detector = _detector()

        with patch(
            "agentit.portal.github_pr.get_commits_behind",
            return_value={"ahead_by": 5, "behind_by": 0, "status": "ahead", "hours_behind": 2.0},
        ):
            await detector.detect_once()

        publish_calls = [
            c for c in detector._publisher.publish.call_args_list
            if c.kwargs.get("action") == "gitops-lag-detected"
        ]
        assert len(publish_calls) == 1
        assert publish_calls[0].kwargs["severity"] == "critical"
        assert publish_calls[0].kwargs["target_app"] == "agentit"

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_no_alert_when_in_sync(self, mock_list):
        mock_list.return_value = [_AGENTIT_APP]
        detector = _detector()

        with patch(
            "agentit.portal.github_pr.get_commits_behind",
            return_value={"ahead_by": 0, "behind_by": 0, "status": "identical", "hours_behind": None},
        ):
            await detector.detect_once()

        publish_calls = [
            c for c in detector._publisher.publish.call_args_list
            if c.kwargs.get("action") == "gitops-lag-detected"
        ]
        assert publish_calls == []

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_no_alert_when_slightly_behind_and_recent(self, mock_list):
        """A couple of commits landed a few minutes ago (normal CI-in-flight
        lag) must not alert -- both thresholds (commits AND hours) need to
        be under the bar."""
        mock_list.return_value = [_AGENTIT_APP]
        detector = _detector()

        with patch(
            "agentit.portal.github_pr.get_commits_behind",
            return_value={"ahead_by": 1, "behind_by": 0, "status": "ahead", "hours_behind": 0.05},
        ):
            await detector.detect_once()

        publish_calls = [
            c for c in detector._publisher.publish.call_args_list
            if c.kwargs.get("action") == "gitops-lag-detected"
        ]
        assert publish_calls == []

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_alerts_on_hours_threshold_alone_even_with_few_commits(self, mock_list):
        """A single commit stuck for a long time is just as real an
        incident as a burst of commits -- either threshold alone alerts."""
        mock_list.return_value = [_AGENTIT_APP]
        detector = _detector()

        with patch(
            "agentit.portal.github_pr.get_commits_behind",
            return_value={"ahead_by": 1, "behind_by": 0, "status": "ahead", "hours_behind": 5.0},
        ):
            await detector.detect_once()

        publish_calls = [
            c for c in detector._publisher.publish.call_args_list
            if c.kwargs.get("action") == "gitops-lag-detected"
        ]
        assert len(publish_calls) == 1

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_skips_non_agentit_apps(self, mock_list):
        """Self-check only -- guessing another fleet app's default branch
        would violate this repo's "never fabricate data" rule."""
        other_app = {
            "metadata": {"name": "some-other-app"},
            "spec": {"source": {"repoURL": "https://github.com/example/other.git"}},
            "status": {"sync": {"status": "Synced", "revision": "b" * 40}, "health": {"status": "Healthy"}},
        }
        mock_list.return_value = [other_app]
        detector = _detector()

        with patch("agentit.portal.github_pr.get_commits_behind") as mock_lag:
            await detector.detect_once()

        mock_lag.assert_not_called()

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    async def test_github_api_failure_does_not_crash_tick(self, mock_list):
        mock_list.return_value = [_AGENTIT_APP]
        detector = _detector()

        with patch("agentit.portal.github_pr.get_commits_behind", return_value={}):
            await detector.detect_once()  # must not raise


class TestAutoSyncLogged:
    """docs/ledger-design-spec.md Phase 0: an auto-sync attempt (success or
    failure) must be persisted via log_event(), not only a click.echo() --
    otherwise it's invisible everywhere in the Ledger/Events/Timeline."""

    @patch("agentit.watchers.drift_detector.kube.custom_objects")
    async def test_successful_auto_sync_logs_drift_auto_synced_event(self, mock_custom_objects):
        async_store, store = await make_async_store()
        await store.set_setting("auto_mode", "true")
        detector = DriftDetector(publisher=MagicMock(), interval=1, store=async_store)

        await detector._maybe_auto_sync("some-app")

        mock_custom_objects.return_value.patch_namespaced_custom_object.assert_called_once()
        events = await store.list_events()
        synced = [e for e in events if e["action"] == "drift-auto-synced"]
        assert len(synced) == 1
        assert synced[0]["target_app"] == "some-app"
        assert synced[0]["severity"] == "info"

    @patch("agentit.watchers.drift_detector.kube.custom_objects")
    async def test_failed_auto_sync_logs_drift_auto_sync_failed_event(self, mock_custom_objects):
        mock_custom_objects.return_value.patch_namespaced_custom_object.side_effect = RuntimeError(
            "connection refused"
        )
        async_store, store = await make_async_store()
        await store.set_setting("auto_mode", "true")
        detector = DriftDetector(publisher=MagicMock(), interval=1, store=async_store)

        await detector._maybe_auto_sync("some-app")  # must not raise

        events = await store.list_events()
        failed = [e for e in events if e["action"] == "drift-auto-sync-failed"]
        assert len(failed) == 1
        assert failed[0]["target_app"] == "some-app"
        assert failed[0]["severity"] == "warning"
        assert "connection refused" in failed[0]["summary"]


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.drift_detector.asyncio.sleep", side_effect=KeyboardInterrupt)
    @patch("agentit.watchers.drift_detector.kube.list_custom_resources", return_value=None)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_list, mock_sleep, capsys):
        detector = _detector()
        await detector.run()

        captured = capsys.readouterr()
        assert "Starting drift detector" in captured.err
        assert "Drift detector stopped." in captured.err
        mock_sleep.assert_called_once_with(1)


class TestTickRunsOffEventLoop:
    """``detect_once`` is now a genuine coroutine (part of this pass's
    FleetOrchestrator/AutoMode/RemediationDispatcher/RemediationLoop async
    rewrite, which forced DriftDetector's own AutoMode call site to become
    async too) -- ``run()`` awaits it directly rather than dispatching the
    whole tick to a worker thread. The specific blocking kube call inside
    ``detect_once`` (``_fetch_argo_apps``, which wraps
    ``kube.list_custom_resources``) is still narrowly wrapped in
    ``asyncio.to_thread`` so it doesn't block the event loop, and
    record_tick telemetry must still fire afterwards."""

    @patch("agentit.watchers.drift_detector.asyncio.sleep", side_effect=KeyboardInterrupt)
    @patch("agentit.watchers.drift_detector.kube.list_custom_resources", return_value=None)
    async def test_detect_once_narrowly_wraps_blocking_kube_call_and_telemetry_records(self, mock_list, mock_sleep):
        async_store, raw_store = await make_async_store()
        detector = DriftDetector(publisher=MagicMock(), interval=1, store=async_store)

        with patch(
            "agentit.watchers.drift_detector.asyncio.to_thread", wraps=asyncio.to_thread
        ) as mock_to_thread:
            await detector.run()

        mock_to_thread.assert_any_call(detector._fetch_argo_apps)
        events = await raw_store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)


class TestConstructionAcceptsAsyncStoreDirectly:
    """The real bug fixed here: cli.py used to hand DriftDetector
    `store.raw` because `_maybe_auto_sync` always re-wrapped it. Now the
    store is genuinely async-compatible throughout, so a store constructed
    via `create_store()`'s own facade must work end to end."""

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources", return_value=None)
    async def test_detect_once_works_against_create_store_facade(self, mock_list, postgres_dsn):
        from agentit.portal.store import create_store

        store = await create_store(postgres_dsn, min_size=1, max_size=2)
        await store.set_setting("auto_mode", "false")
        detector = DriftDetector(publisher=MagicMock(), interval=1, store=store)
        result = await detector.detect_once()  # must not raise AttributeError/TypeError
        assert result == []
