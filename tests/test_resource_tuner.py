from __future__ import annotations

from unittest.mock import patch

from agentit.resource_tuner import analyze_resource_usage, ResourceRecommendation


class TestResourceTuner:
    @patch("agentit.resource_tuner.query_prometheus", return_value=None)
    def test_no_data_returns_empty(self, mock_prom):
        recs = analyze_resource_usage("myapp", "default")
        assert isinstance(recs, list)
        assert len(recs) == 0

    @patch("agentit.resource_tuner.query_prometheus")
    def test_over_provisioned_cpu(self, mock_prom):
        def side_effect(query, timeout=10):
            if "cpu_usage" in query and "avg" in query:
                return 0.01  # 10m actual
            if "resource_requests" in query and "cpu" in query:
                return 0.5   # 500m requested
            return None
        mock_prom.side_effect = side_effect
        recs = analyze_resource_usage("myapp", "default")
        cpu_recs = [r for r in recs if r.resource_type == "cpu_request"]
        assert len(cpu_recs) == 1
        assert "over-provisioned" in cpu_recs[0].reason

    @patch("agentit.resource_tuner.query_prometheus")
    def test_over_provisioned_memory(self, mock_prom):
        def side_effect(query, timeout=10):
            if "memory_working_set" in query and "avg" in query:
                return 50 * 1024 * 1024   # 50Mi actual
            if "resource_requests" in query and "memory" in query:
                return 512 * 1024 * 1024  # 512Mi requested
            if "memory_working_set" in query and "max" in query:
                return 80 * 1024 * 1024
            return None
        mock_prom.side_effect = side_effect
        recs = analyze_resource_usage("myapp", "default")
        mem_recs = [r for r in recs if r.resource_type == "memory_request"]
        assert len(mem_recs) == 1
        assert "over-provisioned" in mem_recs[0].reason

    def test_recommendation_dataclass(self):
        r = ResourceRecommendation("cpu_request", "500m", "100m", "over-provisioned", 0.8)
        assert r.resource_type == "cpu_request"
        assert r.confidence == 0.8
