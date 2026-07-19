"""Tests for agentit.kube — API discovery and rollback behavior."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.config import ConfigException

from agentit import kube


class TestOfflineMode:
    """``AGENTIT_OFFLINE`` guarantees zero cluster access -- regression
    coverage for the finding (hit independently by both a customer-review
    and a UX review pass) that `unset KUBECONFIG` alone is NOT sufficient:
    the Kubernetes Python client's default config-resolution chain still
    falls back to the ambient default ``~/.kube/config`` regardless."""

    def test_get_client_raises_immediately_without_resolving_config(self):
        with patch.dict("os.environ", {"AGENTIT_OFFLINE": "1"}), \
             patch("kubernetes.config.load_incluster_config") as mock_incluster, \
             patch("kubernetes.config.load_kube_config") as mock_kubeconfig:
            with pytest.raises(kube.KubeError, match="AGENTIT_OFFLINE"):
                kube.get_client()

        mock_incluster.assert_not_called()
        mock_kubeconfig.assert_not_called()

    def test_offline_check_runs_before_the_cache_lookup(self):
        """Toggling AGENTIT_OFFLINE mid-session must not be bypassed by an
        already-resolved client cached from before it was set."""
        with patch("agentit.kube._client_cache", MagicMock()), \
             patch("agentit.kube._client_cache_time", kube._time.monotonic()), \
             patch.dict("os.environ", {"AGENTIT_OFFLINE": "1"}):
            with pytest.raises(kube.KubeError, match="AGENTIT_OFFLINE"):
                kube.get_client()

    @pytest.mark.parametrize("value", ["1", "true", "True", "on", "ON"])
    def test_truthy_values_all_trigger_offline_mode(self, value):
        with patch.dict("os.environ", {"AGENTIT_OFFLINE": value}), \
             patch("kubernetes.config.load_incluster_config") as mock_incluster, \
             patch("kubernetes.config.load_kube_config") as mock_kubeconfig:
            with pytest.raises(kube.KubeError, match="AGENTIT_OFFLINE"):
                kube.get_client()

        mock_incluster.assert_not_called()
        mock_kubeconfig.assert_not_called()

    def test_falsy_value_does_not_enable_offline_mode(self):
        with patch.dict("os.environ", {"AGENTIT_OFFLINE": "0"}), \
             patch("agentit.kube._client_cache", None), \
             patch("kubernetes.config.load_incluster_config", side_effect=ConfigException("no in-cluster config")), \
             patch("kubernetes.config.load_kube_config") as mock_kubeconfig:
            kube.get_client()

        mock_kubeconfig.assert_called_once()

    def test_unset_leaves_existing_behavior_unchanged(self, monkeypatch):
        """Without AGENTIT_OFFLINE set at all, get_client() must still
        attempt real config resolution exactly as it did before this
        change (mocked here so this hermetic test never itself attempts a
        real config-resolution call)."""
        monkeypatch.delenv("AGENTIT_OFFLINE", raising=False)
        with patch("agentit.kube._client_cache", None), \
             patch("kubernetes.config.load_incluster_config", side_effect=ConfigException("no in-cluster config")) as mock_incluster, \
             patch("kubernetes.config.load_kube_config") as mock_kubeconfig:
            result = kube.get_client()

        mock_incluster.assert_called_once()
        mock_kubeconfig.assert_called_once()
        from kubernetes import client as real_client
        assert result is real_client


class TestGetApiResources:
    def test_queries_core_and_named_api_groups(self):
        """Regression: get_api_resources() previously only queried the core
        v1 group, so kinds like Deployment/Ingress/HPA/NetworkPolicy (which
        live in named API groups) were never reported as available."""
        mock_client = MagicMock()

        # Core v1 resources.
        core_res = MagicMock()
        core_res.kind = "Pod"
        mock_client.CoreV1Api.return_value.get_api_resources.return_value.resources = [core_res]

        # Named API group "apps" with preferred version v1, exposing Deployment.
        group_apps = MagicMock()
        group_apps.name = "apps"
        group_apps.preferred_version.version = "v1"
        group_apps.versions = []

        # Named API group "networking.k8s.io" with only .versions (no preferred_version).
        group_net = MagicMock()
        group_net.name = "networking.k8s.io"
        group_net.preferred_version = None
        ver = MagicMock()
        ver.version = "v1"
        group_net.versions = [ver]

        groups_response = MagicMock()
        groups_response.groups = [group_apps, group_net]
        mock_client.ApisApi.return_value.get_api_versions.return_value = groups_response

        api_client_mock = MagicMock()
        mock_client.ApiClient.return_value = api_client_mock

        def _call_api(path, method, **kwargs):
            resp = MagicMock()
            if path == "/apis/apps/v1":
                resp.read.return_value = json.dumps(
                    {"resources": [{"kind": "Deployment"}, {"kind": "ReplicaSet"}]}
                ).encode()
            elif path == "/apis/networking.k8s.io/v1":
                resp.read.return_value = json.dumps(
                    {"resources": [{"kind": "Ingress"}, {"kind": "NetworkPolicy"}]}
                ).encode()
            else:
                resp.read.return_value = b'{"resources": []}'
            return resp

        api_client_mock.call_api.side_effect = _call_api

        with patch("agentit.kube.get_client", return_value=mock_client):
            resources = kube.get_api_resources()

        assert "pod" in resources
        assert "deployment" in resources
        assert "replicaset" in resources
        assert "ingress" in resources
        assert "networkpolicy" in resources
        # In-cluster discovery must pass BearerToken; without it call_api
        # runs as system:anonymous and named groups 403 (live dogfood bug).
        for call in api_client_mock.call_api.call_args_list:
            assert call.kwargs.get("auth_settings") == ["BearerToken"]

    def test_one_group_failure_does_not_abort_the_rest(self):
        """A single named API group failing discovery must not prevent the
        others (or core v1) from being reported."""
        mock_client = MagicMock()
        core_res = MagicMock()
        core_res.kind = "Pod"
        mock_client.CoreV1Api.return_value.get_api_resources.return_value.resources = [core_res]

        broken_group = MagicMock()
        broken_group.name = "broken.example.com"
        broken_group.preferred_version.version = "v1"
        broken_group.versions = []

        good_group = MagicMock()
        good_group.name = "batch"
        good_group.preferred_version.version = "v1"
        good_group.versions = []

        groups_response = MagicMock()
        groups_response.groups = [broken_group, good_group]
        mock_client.ApisApi.return_value.get_api_versions.return_value = groups_response

        api_client_mock = MagicMock()
        mock_client.ApiClient.return_value = api_client_mock

        def _call_api(path, method, **kwargs):
            if path == "/apis/broken.example.com/v1":
                raise RuntimeError("discovery unavailable")
            resp = MagicMock()
            resp.read.return_value = json.dumps({"resources": [{"kind": "CronJob"}]}).encode()
            return resp

        api_client_mock.call_api.side_effect = _call_api

        with patch("agentit.kube.get_client", return_value=mock_client):
            resources = kube.get_api_resources()

        assert "pod" in resources
        assert "cronjob" in resources

    def test_total_failure_raises_kube_error(self):
        with patch("agentit.kube.get_client", side_effect=RuntimeError("no cluster access")):
            with pytest.raises(kube.KubeError):
                kube.get_api_resources()


class TestGetCurrentClusterIdentity:
    """Regression coverage for the incident where a customer-review agent
    expected zero cluster access after `unset KUBECONFIG` and instead
    silently hit whatever cluster the ambient kubeconfig pointed at, with
    no on-screen indication of which cluster that was."""

    def test_returns_host_and_context_when_kubeconfig_resolves(self):
        with patch("agentit.kube.get_client", return_value=MagicMock()), \
             patch("agentit.kube._client_cache_source", "kubeconfig"), \
             patch("kubernetes.client.Configuration.get_default_copy") as mock_get_default, \
             patch("kubernetes.config.list_kube_config_contexts", return_value=([], {"name": "my-cluster-context"})):
            mock_get_default.return_value.host = "https://api.example.com:6443"
            identity = kube.get_current_cluster_identity()

        assert identity["host"] == "https://api.example.com:6443"
        assert identity["context"] == "my-cluster-context"
        assert identity["in_cluster"] is False
        assert identity["label"] == "https://api.example.com:6443 (context: my-cluster-context)"

    def test_labels_in_cluster_config_distinctly(self):
        with patch("agentit.kube.get_client", return_value=MagicMock()), \
             patch("agentit.kube._client_cache_source", "in-cluster"), \
             patch("kubernetes.client.Configuration.get_default_copy") as mock_get_default:
            mock_get_default.return_value.host = "https://172.30.0.1:443"
            identity = kube.get_current_cluster_identity()

        assert identity["in_cluster"] is True
        assert identity["context"] is None
        assert identity["label"] == "in-cluster (this pod's own cluster)"

    def test_degrades_gracefully_when_no_cluster_reachable(self):
        """No kubeconfig at all (e.g. KUBECONFIG points at a nonexistent
        path and no in-cluster config either) -- get_client() raises. Must
        return a clear unknown/unreachable indicator, never raise."""
        with patch("agentit.kube.get_client", side_effect=RuntimeError("no configuration found")):
            identity = kube.get_current_cluster_identity()

        assert identity == {"label": "unknown/unreachable cluster", "host": None, "context": None, "in_cluster": False}

    def test_degrades_gracefully_when_host_lookup_fails(self):
        """A resolved client whose Configuration still can't produce a host
        (defensive edge case) must fall back to the unknown label rather
        than raising or reporting a blank host as if it were legitimate."""
        with patch("agentit.kube.get_client", return_value=MagicMock()), \
             patch("agentit.kube._client_cache_source", "kubeconfig"), \
             patch("kubernetes.client.Configuration.get_default_copy", side_effect=RuntimeError("no default config")), \
             patch("kubernetes.config.list_kube_config_contexts", side_effect=RuntimeError("no kubeconfig file")):
            identity = kube.get_current_cluster_identity()

        assert identity["host"] is None
        assert identity["context"] is None
        assert identity["label"] == "unknown/unreachable cluster"


class TestRolloutUndo:
    def test_aborts_argo_rollout_when_present(self):
        """For Argo Rollouts-managed apps, rollout_undo must patch the
        Rollout's status subresource (real abort), not just restart pods."""
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {"kind": "Rollout"}
        mock_apps = MagicMock()

        with patch("agentit.kube.custom_objects", return_value=mock_custom), \
             patch("agentit.kube.apps_v1", return_value=mock_apps):
            result = kube.rollout_undo("my-app", "my-ns")

        assert result["success"] is True
        assert "aborted" in result["message"].lower()
        mock_custom.patch_namespaced_custom_object_status.assert_called_once()
        call_args = mock_custom.patch_namespaced_custom_object_status.call_args
        assert call_args[0][:5] == ("argoproj.io", "v1alpha1", "my-ns", "rollouts", "my-app")
        assert call_args[1]["body"] == {"status": {"abort": True}}
        mock_apps.patch_namespaced_deployment.assert_not_called()

    def test_falls_back_to_restart_when_no_rollout_exists(self):
        """Plain Deployments (no matching Rollout) still get the restart
        fallback -- rollout_undo must not error out for non-Rollout apps."""
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.side_effect = RuntimeError("404 not found")
        mock_apps = MagicMock()

        with patch("agentit.kube.custom_objects", return_value=mock_custom), \
             patch("agentit.kube.apps_v1", return_value=mock_apps):
            result = kube.rollout_undo("plain-app", "my-ns")

        assert result["success"] is True
        assert "restart" in result["message"].lower()
        mock_apps.patch_namespaced_deployment.assert_called_once()
        mock_custom.patch_namespaced_custom_object_status.assert_not_called()

    def test_abort_failure_is_reported_not_raised(self):
        mock_custom = MagicMock()
        mock_custom.get_namespaced_custom_object.return_value = {"kind": "Rollout"}
        mock_custom.patch_namespaced_custom_object_status.side_effect = RuntimeError("forbidden")

        with patch("agentit.kube.custom_objects", return_value=mock_custom):
            result = kube.rollout_undo("my-app", "my-ns")

        assert result["success"] is False
        assert "forbidden" in result["message"]


def _cronjob(name: str, schedule: str = "*/10 * * * *", suspend: bool = False,
             last_schedule_time=None, last_successful_time=None, active=None) -> MagicMock:
    cj = MagicMock()
    cj.metadata.name = name
    cj.spec.schedule = schedule
    cj.spec.suspend = suspend
    cj.status.last_schedule_time = last_schedule_time
    cj.status.last_successful_time = last_successful_time
    cj.status.active = active
    return cj


class TestListCronjobs:
    """Backs the self-health-check watcher's maintenance-CronJob check
    (watchers/self_health_check.py) -- generic over whatever CronJobs
    currently exist, not a hardcoded name list."""

    def test_returns_simplified_dicts_for_every_cronjob(self):
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(minutes=5)
        mock_batch = MagicMock()
        mock_batch.list_namespaced_cron_job.return_value = MagicMock(items=[
            _cronjob("tekton-cleanup", last_schedule_time=now, last_successful_time=earlier, active=[object()]),
        ])

        with patch("agentit.kube.batch_v1", return_value=mock_batch):
            result = kube.list_cronjobs("agentit")

        assert result == [{
            "name": "tekton-cleanup",
            "schedule": "*/10 * * * *",
            "suspended": False,
            "last_schedule_time": now.isoformat(),
            "last_successful_time": earlier.isoformat(),
            "active_count": 1,
        }]

    def test_handles_never_scheduled_cronjob(self):
        """A freshly-installed CronJob with no status timestamps yet must
        report None, not raise or fabricate a timestamp."""
        mock_batch = MagicMock()
        mock_batch.list_namespaced_cron_job.return_value = MagicMock(items=[
            _cronjob("secret-rotation"),
        ])

        with patch("agentit.kube.batch_v1", return_value=mock_batch):
            result = kube.list_cronjobs("agentit")

        assert result[0]["last_schedule_time"] is None
        assert result[0]["last_successful_time"] is None
        assert result[0]["active_count"] == 0

    def test_reports_suspended_flag(self):
        mock_batch = MagicMock()
        mock_batch.list_namespaced_cron_job.return_value = MagicMock(items=[
            _cronjob("cost-report", suspend=True),
        ])

        with patch("agentit.kube.batch_v1", return_value=mock_batch):
            result = kube.list_cronjobs("agentit")

        assert result[0]["suspended"] is True

    def test_api_failure_raises_kube_error(self):
        mock_batch = MagicMock()
        mock_batch.list_namespaced_cron_job.side_effect = RuntimeError("connection refused")

        with patch("agentit.kube.batch_v1", return_value=mock_batch):
            with pytest.raises(kube.KubeError):
                kube.list_cronjobs("agentit")


def _pod(phase: str, created: datetime) -> MagicMock:
    pod = MagicMock()
    pod.status.phase = phase
    pod.metadata.creation_timestamp = created
    return pod


class TestCountStaleTerminalPods:
    """Backs the self-health-check watcher's cleanup-effectiveness check --
    a generic "is the terminal-pod backlog actually bounded" signal,
    independent of any one specific cleanup-CronJob bug."""

    def test_counts_only_old_failed_and_succeeded_pods(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=3)
        recent = now - timedelta(minutes=5)
        mock_core = MagicMock()
        mock_core.list_namespaced_pod.return_value = MagicMock(items=[
            _pod("Failed", old),
            _pod("Succeeded", old),
            _pod("Failed", recent),      # too young to count
            _pod("Running", old),        # not terminal -- must not count
        ])

        with patch("agentit.kube.core_v1", return_value=mock_core):
            count = kube.count_stale_terminal_pods("agentit", max_age_hours=2.0)

        assert count == 2

    def test_no_stale_pods_returns_zero(self):
        mock_core = MagicMock()
        mock_core.list_namespaced_pod.return_value = MagicMock(items=[])

        with patch("agentit.kube.core_v1", return_value=mock_core):
            count = kube.count_stale_terminal_pods("agentit")

        assert count == 0

    def test_api_failure_raises_kube_error(self):
        mock_core = MagicMock()
        mock_core.list_namespaced_pod.side_effect = RuntimeError("timeout")

        with patch("agentit.kube.core_v1", return_value=mock_core):
            with pytest.raises(kube.KubeError):
                kube.count_stale_terminal_pods("agentit")
