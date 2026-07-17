"""Tests proving `kube.py`'s real API-calling functions are wired into
`kube_breaker` (`agentit.portal.helpers`) -- see `kube.py`'s
`_kube_breaker_scope()`.

Regression coverage for the finding that `kube_breaker` was registered in
`_ALL_BREAKERS` and shown on the Health page's Circuit Breakers table but
was never actually tripped by a real API failure: `llm_breaker` is wired
into `llm.py`'s `LLMClient._chat()` (`is_open` check + `record_success()`/
`record_failure()` around every real Anthropic call), but grepping
`kube.py` for "breaker" previously returned nothing, so the Health page's
"kube" row was permanently meaningless -- it could never open no matter
how many times real Kubernetes API calls failed.

Every test here mocks `kube.py`'s low-level client accessors
(`core_v1`, `custom_objects`, `batch_v1`, `dynamic_client`) -- none of
these ever attempt a real cluster connection, satisfying the "never let
testing reach a real cluster" constraint independent of the module-level
`AGENTIT_OFFLINE`/`KUBECONFIG` safety net already in place for this run.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from agentit import kube
from agentit.portal.helpers import kube_breaker


def _api_exc(status: int, message: str = "boom") -> ApiException:
    exc = ApiException(status=status, reason=message)
    exc.body = f'{{"message": "{message}"}}'
    return exc


class TestBreakerOpensAfterThreshold:
    """(a) repeated real API-call failures (mocked) actually open
    `kube_breaker` after its threshold."""

    def test_five_consecutive_failures_open_the_breaker(self):
        assert kube_breaker._threshold == 5  # documents the threshold this test proves against
        mock_core_v1 = MagicMock()
        mock_core_v1.list_namespaced_pod.side_effect = RuntimeError("connection refused")

        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            for _ in range(kube_breaker._threshold):
                assert not kube_breaker.is_open
                with pytest.raises(kube.KubeError):
                    kube.list_pods("my-ns")

        assert kube_breaker.is_open
        assert kube_breaker._failures == kube_breaker._threshold

    def test_fewer_than_threshold_failures_leave_the_breaker_closed(self):
        mock_core_v1 = MagicMock()
        mock_core_v1.list_namespaced_pod.side_effect = RuntimeError("timeout")

        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            for _ in range(kube_breaker._threshold - 1):
                with pytest.raises(kube.KubeError):
                    kube.list_pods("my-ns")

        assert not kube_breaker.is_open

    def test_failures_from_different_functions_accumulate_on_the_shared_breaker(self):
        """`kube_breaker` is one shared instance -- failures from
        different real-API-calling functions must all count toward the
        same threshold, not be tracked per-function."""
        mock_custom = MagicMock()
        mock_custom.list_cluster_custom_object.side_effect = RuntimeError("apiserver unreachable")
        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespace.side_effect = RuntimeError("apiserver unreachable")

        with patch("agentit.kube.custom_objects", return_value=mock_custom), \
             patch("agentit.kube.core_v1", return_value=mock_core_v1):
            for _ in range(3):
                with pytest.raises(kube.KubeError):
                    kube.list_custom_resources("argoproj.io", "v1alpha1", "applications")
            assert not kube_breaker.is_open
            for _ in range(2):
                with pytest.raises(kube.KubeError):
                    kube.namespace_exists("my-ns")

        assert kube_breaker.is_open


class TestOpenBreakerSkipsRealCalls:
    """(b) once open, further calls are skipped/fail-fast rather than
    attempting the real API -- checked against each function's own
    existing failure contract (raise KubeError / return a safe fallback)."""

    def test_list_pods_raises_without_calling_the_real_api(self):
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()

        mock_core_v1 = MagicMock()
        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            with pytest.raises(kube.KubeError, match="circuit breaker"):
                kube.list_pods("my-ns")

        mock_core_v1.list_namespaced_pod.assert_not_called()

    def test_namespace_exists_raises_without_calling_the_real_api(self):
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()

        mock_core_v1 = MagicMock()
        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            with pytest.raises(kube.KubeError, match="circuit breaker"):
                kube.namespace_exists("my-ns")

        mock_core_v1.read_namespace.assert_not_called()

    def test_apply_yaml_returns_safe_fallback_without_calling_the_real_api(self):
        """`apply_yaml`'s contract never raises for a bad manifest/cluster
        state -- it returns a structured dict -- so the breaker-open
        fallback matches that instead of raising."""
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()

        mock_dyn = MagicMock()
        with patch("agentit.kube.dynamic_client", return_value=mock_dyn):
            result = kube.apply_yaml(
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", "default",
            )

        assert result["applied"] is False
        assert "circuit breaker" in result["error"].lower()
        mock_dyn.resources.get.assert_not_called()

    def test_create_config_map_returns_false_without_calling_the_real_api(self):
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()

        mock_core_v1 = MagicMock()
        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            result = kube.create_config_map("cm", "default", {"a": "b"})

        assert result is False
        mock_core_v1.create_namespaced_config_map.assert_not_called()

    def test_rollout_undo_returns_failure_dict_without_calling_the_real_api(self):
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()

        mock_custom = MagicMock()
        with patch("agentit.kube.custom_objects", return_value=mock_custom):
            result = kube.rollout_undo("app", "default")

        assert result["success"] is False
        assert "circuit breaker" in result["message"].lower()
        mock_custom.get_namespaced_custom_object.assert_not_called()

    def test_get_job_status_returns_unknown_without_calling_the_real_api(self):
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()

        mock_batch_v1 = MagicMock()
        with patch("agentit.kube.batch_v1", return_value=mock_batch_v1):
            result = kube.get_job_status("job", "default")

        assert result == "unknown"
        mock_batch_v1.read_namespaced_job_status.assert_not_called()


class TestSuccessResetsFailureCount:
    """(c) a success resets the failure count."""

    def test_success_after_failures_resets_the_count(self):
        mock_core_v1 = MagicMock()
        mock_core_v1.list_namespaced_pod.side_effect = RuntimeError("timeout")

        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            for _ in range(kube_breaker._threshold - 1):
                with pytest.raises(kube.KubeError):
                    kube.list_pods("my-ns")
            assert kube_breaker._failures == kube_breaker._threshold - 1
            assert not kube_breaker.is_open

            mock_core_v1.list_namespaced_pod.side_effect = None
            mock_core_v1.list_namespaced_pod.return_value = MagicMock(items=[])
            result = kube.list_pods("my-ns")

        assert result == []
        assert kube_breaker._failures == 0
        assert not kube_breaker.is_open

    def test_benign_404_on_get_custom_resource_never_counts_as_a_failure(self):
        """A 404 "not found" is an expected outcome for a lookup, not a
        cluster-health signal -- repeated 404s must never open the
        breaker, however many of them happen in a row."""
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = _api_exc(404, "not found")

        with patch("agentit.kube.custom_objects", return_value=mock_custom):
            for _ in range(kube_breaker._threshold * 2):
                assert kube.get_custom_resource("argoproj.io", "v1alpha1", "applications", "missing") is None

        assert kube_breaker._failures == 0
        assert not kube_breaker.is_open

    def test_benign_409_on_create_namespace_never_counts_as_a_failure(self):
        """A 409 "already exists" on create_namespace is treated as
        success (no-op) by the function's own contract -- it must not
        count against the breaker either."""
        mock_core_v1 = MagicMock()
        mock_core_v1.create_namespace.side_effect = _api_exc(409, "already exists")

        with patch("agentit.kube.core_v1", return_value=mock_core_v1):
            for _ in range(kube_breaker._threshold * 2):
                kube.create_namespace("my-ns")  # returns None, does not raise

        assert kube_breaker._failures == 0
        assert not kube_breaker.is_open

    def test_409_conflict_on_apply_yaml_never_counts_as_a_failure(self):
        """A field-manager conflict is an ownership dispute, not a
        cluster-health problem -- `apply_yaml` must not let repeated
        conflicts open the breaker."""
        dyn = MagicMock()
        resource = MagicMock()
        resource.namespaced = True
        dyn.resources.get.return_value = resource
        dyn.server_side_apply.side_effect = _api_exc(409, "field-manager conflict")
        content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: default\n"

        with patch("agentit.kube.dynamic_client", return_value=dyn):
            for _ in range(kube_breaker._threshold * 2):
                result = kube.apply_yaml(content, "default")
                assert result["conflict"] is True

        assert kube_breaker._failures == 0
        assert not kube_breaker.is_open


class TestOfflineModeNeverTripsBreaker:
    """(d) `AGENTIT_OFFLINE` mode doesn't trip the breaker."""

    def test_repeated_offline_calls_do_not_open_the_breaker(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_OFFLINE", "1")
        for _ in range(kube_breaker._threshold * 2):
            with pytest.raises(kube.KubeError, match="AGENTIT_OFFLINE"):
                kube.list_pods("my-ns")

        assert kube_breaker._failures == 0
        assert not kube_breaker.is_open

    def test_offline_error_is_distinguishable_from_a_real_api_failure(self, monkeypatch):
        """`get_client()` raises a dedicated `KubeOfflineError` subclass
        specifically so `_kube_breaker_scope()` can tell "explicit offline
        hard-stop" apart from "real API failure" -- this is what makes the
        carve-out in the previous test possible without string-sniffing
        exception messages."""
        monkeypatch.setenv("AGENTIT_OFFLINE", "1")
        with pytest.raises(kube.KubeOfflineError):
            kube.get_client()
        assert issubclass(kube.KubeOfflineError, kube.KubeError)

    def test_offline_mode_takes_priority_over_an_already_open_breaker(self, monkeypatch):
        """Whichever check fires, the caller still gets a clear KubeError
        and the breaker's failure count is never perturbed by offline
        mode -- true whether the breaker was already open from real
        failures or not."""
        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()
        monkeypatch.setenv("AGENTIT_OFFLINE", "1")

        with pytest.raises(kube.KubeError):
            kube.list_pods("my-ns")

        assert kube_breaker._failures == kube_breaker._threshold  # unchanged, not incremented further
