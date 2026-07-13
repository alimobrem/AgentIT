"""Tests for agentit.slo_collector -- metric collection and breach direction."""
from __future__ import annotations

from unittest.mock import patch

from agentit.slo_collector import collect_slo, is_breached


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
    def test_no_collector_for_latency_returns_none(self):
        """latency_p99_ms has no cluster-side collector -- must return None
        (so callers can log/skip clearly) rather than raising."""
        assert collect_slo("latency_p99_ms", "some-namespace") is None

    @patch("agentit.slo_collector._collect_error_rate", return_value=1.5)
    def test_error_rate_dispatches_to_collector(self, mock_fn):
        assert collect_slo("error_rate", "ns") == 1.5
        mock_fn.assert_called_once_with("ns")

    @patch("agentit.slo_collector._collect_availability", return_value=99.5)
    def test_availability_dispatches_to_collector(self, mock_fn):
        assert collect_slo("availability", "ns") == 99.5
        mock_fn.assert_called_once_with("ns")
