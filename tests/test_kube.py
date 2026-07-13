"""Tests for agentit.kube — API discovery and rollback behavior."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agentit import kube


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
