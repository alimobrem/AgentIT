"""Tests for agentit.slo_collector -- metric collection and breach direction."""
from __future__ import annotations

import math
from unittest.mock import patch

from agentit.slo_collector import _collect_latency_p99, collect_slo, is_breached


class TestIsBreached:
    def test_availability_breached_below_target(self):
        assert is_breached("availability", 95.0, 99.9) is True

    def test_availability_not_breached_above_target(self):
        assert is_breached("availability", 99.99, 99.9) is False

    def test_error_rate_breached_above_target(self):
        assert is_breached("error_rate", 5.0, 0.5) is True

    def test_error_rate_not_breached_below_target(self):
        assert is_breached("error_rate", 0.1, 0.5) is False

    def test_latency_breached_above_target(self):
        assert is_breached("latency_p99_ms", 500.0, 200.0) is True

    def test_latency_not_breached_below_target(self):
        assert is_breached("latency_p99_ms", 100.0, 200.0) is False

    def test_unknown_metric_defaults_to_lower_is_better(self):
        assert is_breached("custom_metric", 10.0, 5.0) is True
        assert is_breached("custom_metric", 3.0, 5.0) is False


class TestCollectSlo:
    def test_no_collector_for_unknown_metric_returns_none(self):
        """A metric type with no registered collector at all must return
        None (so callers can log/skip clearly) rather than raising."""
        assert collect_slo("made_up_metric", "some-namespace") is None

    @patch("agentit.slo_collector._collect_error_rate", return_value=1.5)
    def test_error_rate_dispatches_to_collector(self, mock_fn):
        assert collect_slo("error_rate", "ns") == 1.5
        mock_fn.assert_called_once_with("ns")

    @patch("agentit.slo_collector._collect_availability", return_value=99.5)
    def test_availability_dispatches_to_collector(self, mock_fn):
        assert collect_slo("availability", "ns") == 99.5
        mock_fn.assert_called_once_with("ns")

    @patch("agentit.slo_collector._collect_latency_p99", return_value=120.0)
    def test_latency_dispatches_to_collector(self, mock_fn):
        assert collect_slo("latency_p99_ms", "ns") == 120.0
        mock_fn.assert_called_once_with("ns")


class TestCollectLatencyP99:
    """latency_p99_ms is collected from Prometheus (histogram_quantile over
    http_request_duration_seconds_bucket, scoped to the app's namespace) --
    the same in-cluster Prometheus connection used by resource_tuner, mocked
    at the same `query_prometheus` seam its own tests use."""

    @patch("agentit.slo_collector.query_prometheus", return_value=0.25)
    def test_converts_seconds_to_milliseconds(self, mock_prom):
        assert _collect_latency_p99("my-app") == 250.0

    @patch("agentit.slo_collector.query_prometheus")
    def test_query_is_histogram_quantile_scoped_to_namespace(self, mock_prom):
        mock_prom.return_value = 0.1
        _collect_latency_p99("my-app")
        query = mock_prom.call_args[0][0]
        assert "histogram_quantile(0.99" in query
        assert "http_request_duration_seconds_bucket" in query
        assert 'namespace="my-app"' in query

    @patch("agentit.slo_collector.query_prometheus", return_value=None)
    def test_no_data_returns_none_not_fabricated_value(self, mock_prom):
        """A brand-new app with zero traffic yet -- Prometheus has no
        matching series -- must return None, never a fabricated 0ms."""
        assert _collect_latency_p99("brand-new-app") is None

    @patch("agentit.slo_collector.query_prometheus", return_value=math.nan)
    def test_nan_result_returns_none(self, mock_prom):
        """histogram_quantile can return NaN (e.g. 0/0) for an app with
        buckets registered but no samples -- must also be treated as
        no-data, not passed through as a fabricated NaN 'value'."""
        assert _collect_latency_p99("no-samples-app") is None
