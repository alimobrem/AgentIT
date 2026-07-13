"""Tests for the closed remediation loop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.remediation_loop import RemediationLoop
from conftest import make_async_store, make_report


class TestRemediationLoop:
    async def test_assess_failure_returns_failed(self):
        loop = RemediationLoop(portal_url="http://bad-host:9999", timeout=2)
        result = await loop.trigger("https://github.com/org/app", "app", reason="test")
        assert result["outcome"] == "failed"
        assert result["step"] == "assess"
        await loop.close()

    async def test_trigger_logs_events(self):
        store, raw = make_async_store()
        loop = RemediationLoop(portal_url="http://bad-host:9999", store=store, timeout=2)
        await loop.trigger("https://github.com/org/app", "test-app", reason="test")
        events = raw.list_events()
        assert any(e["action"] == "loop-started" for e in events)
        await loop.close()

    async def test_verify_slos_healthy(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        raw.save_slo(aid, "availability", 99.9)
        raw.update_slo(raw.list_slos(aid)[0]["id"], 99.95, "met")

        loop = RemediationLoop(store=store, timeout=2)
        with patch.object(loop, "_verify_slos") as mock:
            mock.return_value = {"healthy": True, "reason": "All good"}
            result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is True
        await loop.close()

    @patch("agentit.slo_collector.collect_slo")
    async def test_verify_slos_breached(self, mock_collect):
        mock_collect.return_value = 5.0  # 5% error rate, well above 0.01 target
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        raw.save_slo(aid, "error_rate", 0.01)

        loop = RemediationLoop(store=store, timeout=2)
        result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is False
        assert "error_rate" in result["reason"]
        await loop.close()

    @patch("agentit.slo_collector.collect_slo")
    async def test_verify_slos_availability_above_target_is_healthy(self, mock_collect):
        """Regression: availability is higher-is-better -- a current value
        ABOVE target must never be flagged as a breach. The old code did
        `value > target_value` for every metric, which would have wrongly
        breached this (99.99 > 99.9)."""
        mock_collect.return_value = 99.99
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        raw.save_slo(aid, "availability", 99.9)

        loop = RemediationLoop(store=store, timeout=2)
        result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is True
        await loop.close()

    @patch("agentit.slo_collector.collect_slo")
    async def test_verify_slos_availability_below_target_is_breached(self, mock_collect):
        """Availability breach direction: a value BELOW target is unhealthy."""
        mock_collect.return_value = 95.0
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        raw.save_slo(aid, "availability", 99.9)

        loop = RemediationLoop(store=store, timeout=2)
        result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is False
        assert "availability" in result["reason"]
        await loop.close()
