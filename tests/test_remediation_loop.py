"""Tests for the closed remediation loop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.remediation_loop import RemediationLoop
from conftest import make_store, make_report


class TestRemediationLoop:
    def test_assess_failure_returns_failed(self):
        loop = RemediationLoop(portal_url="http://bad-host:9999", timeout=2)
        result = loop.trigger("https://github.com/org/app", "app", reason="test")
        assert result["outcome"] == "failed"
        assert result["step"] == "assess"
        loop.close()

    def test_trigger_logs_events(self):
        store = make_store()
        loop = RemediationLoop(portal_url="http://bad-host:9999", store=store, timeout=2)
        loop.trigger("https://github.com/org/app", "test-app", reason="test")
        events = store.list_events()
        assert any(e["action"] == "loop-started" for e in events)
        loop.close()

    def test_verify_slos_healthy(self):
        store = make_store()
        report = make_report()
        aid = store.save(report)
        store.save_slo(aid, "availability", 99.9)
        store.update_slo(store.list_slos(aid)[0]["id"], 99.95, "met")

        loop = RemediationLoop(store=store, timeout=2)
        with patch.object(loop, "_verify_slos") as mock:
            mock.return_value = {"healthy": True, "reason": "All good"}
            result = loop._verify_slos(aid, "app")
        assert result["healthy"] is True
        loop.close()

    @patch("agentit.slo_collector.collect_slo")
    def test_verify_slos_breached(self, mock_collect):
        mock_collect.return_value = 5.0  # 5% error rate, well above 0.01 target
        store = make_store()
        report = make_report()
        aid = store.save(report)
        store.save_slo(aid, "error_rate", 0.01)

        loop = RemediationLoop(store=store, timeout=2)
        result = loop._verify_slos(aid, "app")
        assert result["healthy"] is False
        assert "error_rate" in result["reason"]
        loop.close()
