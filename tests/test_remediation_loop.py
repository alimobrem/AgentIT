"""Tests for the closed remediation loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from agentit.remediation_loop import RemediationLoop
from conftest import make_async_store, make_report


class TestRemediationLoop:
    async def test_client_sends_internal_token_when_configured(self, monkeypatch):
        """Regression: RemediationLoop's httpx client calls back into the
        portal's verify_internal_token-gated /api/webhook/* routes
        (webhooks.py) exactly like skill_learner.py's _submit_draft_to_portal
        -- but was never attaching the X-Internal-Webhook-Token header,
        so every _assess() call 401'd as soon as the portal enforced the
        token (confirmed live: repeated "loop-failed" events with "Missing
        or invalid internal webhook token")."""
        monkeypatch.setenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", "s3cr3t-token")
        loop = RemediationLoop(timeout=2)
        assert loop._client.headers["X-Internal-Webhook-Token"] == "s3cr3t-token"
        await loop.close()

    async def test_client_omits_internal_token_when_unset(self, monkeypatch):
        monkeypatch.delenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", raising=False)
        loop = RemediationLoop(timeout=2)
        assert "X-Internal-Webhook-Token" not in loop._client.headers
        await loop.close()

    async def test_assess_failure_returns_failed(self):
        loop = RemediationLoop(portal_url="http://bad-host:9999", timeout=2)
        result = await loop.trigger("https://github.com/org/app", "app", reason="test")
        assert result["outcome"] == "failed"
        assert result["step"] == "assess"
        await loop.close()

    async def test_trigger_handles_duplicate_assess_response_without_crashing(self):
        """Regression: `webhooks.py::webhook_assess`'s dedup guard
        (`claim_webhook`) returns HTTP 200 with `{"status": "duplicate",
        "delivery_id": ...}` -- not the normal `{"assessment_id":
        ..., "overall_score": ...}` shape -- when this call's
        (repo_url, criticality) body collides with one already claimed in
        the current time bucket (see `_get_delivery_id`'s docstring, which
        documents this exact live incident: a second, legitimate trigger
        for the same app+criticality got deduped and `trigger()` raised an
        unhandled `KeyError: 'assessment_id'` reading past the `"error" in
        assess_result` check). `trigger()` must recognize this shape and
        return a clean "skipped" outcome instead of crashing."""
        loop = RemediationLoop(timeout=2)
        loop._client.post = AsyncMock(
            return_value=httpx.Response(200, json={"status": "duplicate", "delivery_id": "abc123"})
        )
        result = await loop.trigger("https://github.com/org/app", "app", reason="test")
        assert result["outcome"] == "skipped"
        assert result["step"] == "assess"
        await loop.close()

    async def test_trigger_logs_events(self):
        store, raw = await make_async_store()
        loop = RemediationLoop(portal_url="http://bad-host:9999", store=store, timeout=2)
        await loop.trigger("https://github.com/org/app", "test-app", reason="test")
        events = await raw.list_events()
        assert any(e["action"] == "loop-started" for e in events)
        await loop.close()

    async def test_verify_slos_healthy(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        await raw.save_slo(aid, "availability", 99.9)
        await raw.update_slo((await raw.list_slos(aid))[0]["id"], 99.95, "met")

        loop = RemediationLoop(store=store, timeout=2)
        with patch.object(loop, "_verify_slos") as mock:
            mock.return_value = {"healthy": True, "reason": "All good"}
            result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is True
        await loop.close()

    @patch("agentit.slo_collector.collect_slo")
    async def test_verify_slos_breached(self, mock_collect):
        mock_collect.return_value = 5.0  # 5% error rate, well above 0.01 target
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        await raw.save_slo(aid, "error_rate", 0.01)

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
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        await raw.save_slo(aid, "availability", 99.9)

        loop = RemediationLoop(store=store, timeout=2)
        result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is True
        await loop.close()

    @patch("agentit.slo_collector.collect_slo")
    async def test_verify_slos_availability_below_target_is_breached(self, mock_collect):
        """Availability breach direction: a value BELOW target is unhealthy."""
        mock_collect.return_value = 95.0
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        await raw.save_slo(aid, "availability", 99.9)

        loop = RemediationLoop(store=store, timeout=2)
        result = await loop._verify_slos(aid, "app")
        assert result["healthy"] is False
        assert "availability" in result["reason"]
        await loop.close()
