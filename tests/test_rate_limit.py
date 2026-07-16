"""Unit tests for the opt-in in-memory rate limiter (rate_limit.py) and its
wiring into app.py's middleware -- see docs/deployment.md and
chart/values.yaml's rateLimit block for what this is/isn't a substitute for.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agentit.portal import rate_limit


@pytest.fixture(autouse=True)
def _reset_buckets():
    rate_limit._hits.clear()
    rate_limit._calls_since_purge = 0
    yield
    rate_limit._hits.clear()
    rate_limit._calls_since_purge = 0


class TestIsEnabled:
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTIT_RATE_LIMIT_ENABLED", None)
            assert rate_limit.is_enabled() is False

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "true"})
    def test_enabled_when_set_true(self):
        assert rate_limit.is_enabled() is True

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "false"})
    def test_disabled_when_set_false(self):
        assert rate_limit.is_enabled() is False


class TestCheckRateLimit:
    def test_disabled_always_allows(self):
        """No env var set -- matches the chart's disabled-by-default posture,
        never changes behavior for an existing deployment."""
        for _ in range(1000):
            assert rate_limit.check_rate_limit("1.2.3.4", "/api/webhook/assess") is True

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "true", "AGENTIT_RATE_LIMIT_WEBHOOK_PER_MIN": "3"})
    def test_webhook_path_blocked_after_limit(self):
        client = "1.2.3.4"
        path = "/api/webhook/assess"
        assert rate_limit.check_rate_limit(client, path) is True
        assert rate_limit.check_rate_limit(client, path) is True
        assert rate_limit.check_rate_limit(client, path) is True
        assert rate_limit.check_rate_limit(client, path) is False

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "true", "AGENTIT_RATE_LIMIT_DEFAULT_PER_MIN": "2"})
    def test_default_path_uses_default_limit_not_webhook_limit(self):
        client = "1.2.3.4"
        path = "/fleet"
        assert rate_limit.check_rate_limit(client, path) is True
        assert rate_limit.check_rate_limit(client, path) is True
        assert rate_limit.check_rate_limit(client, path) is False

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "true", "AGENTIT_RATE_LIMIT_DEFAULT_PER_MIN": "1"})
    def test_different_clients_have_independent_buckets(self):
        assert rate_limit.check_rate_limit("client-a", "/fleet") is True
        assert rate_limit.check_rate_limit("client-a", "/fleet") is False
        # A different client key must not be affected by client-a's usage.
        assert rate_limit.check_rate_limit("client-b", "/fleet") is True

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "true", "AGENTIT_RATE_LIMIT_DEFAULT_PER_MIN": "1"})
    def test_webhook_and_default_buckets_are_independent_for_same_client(self):
        client = "1.2.3.4"
        assert rate_limit.check_rate_limit(client, "/fleet") is True
        assert rate_limit.check_rate_limit(client, "/fleet") is False
        # Same client, but a webhook path is tracked in a separate bucket/limit.
        with patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_WEBHOOK_PER_MIN": "5"}):
            assert rate_limit.check_rate_limit(client, "/api/webhook/assess") is True

    @patch.dict(os.environ, {"AGENTIT_RATE_LIMIT_ENABLED": "true", "AGENTIT_RATE_LIMIT_DEFAULT_PER_MIN": "0"})
    def test_exempt_paths_never_limited_even_at_zero_limit(self):
        client = "1.2.3.4"
        for path in ("/healthz", "/readyz", "/metrics"):
            for _ in range(5):
                assert rate_limit.check_rate_limit(client, path) is True


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, headers, client_host):
        self.headers = headers
        self.client = _FakeClient(client_host) if client_host else None


class TestClientKeyFor:
    def test_uses_x_forwarded_for_first_hop(self):
        req = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, "10.0.0.1")
        assert rate_limit.client_key_for(req) == "203.0.113.5"

    def test_falls_back_to_client_host_when_no_forwarded_header(self):
        req = _FakeRequest({}, "10.0.0.7")
        assert rate_limit.client_key_for(req) == "10.0.0.7"

    def test_falls_back_to_unknown_when_no_client(self):
        req = _FakeRequest({}, None)
        assert rate_limit.client_key_for(req) == "unknown"


class TestMiddlewareIntegration:
    """End-to-end through the actual FastAPI middleware stack (app.py)."""

    async def test_rate_limited_response_is_429(self, portal_client):
        # Hit `/ledger` directly — `/` 302s to Ledger and would consume 2
        # requests per logical call once redirect-following is accounted for.
        client, _, _ = portal_client
        with patch.dict(os.environ, {
            "AGENTIT_RATE_LIMIT_ENABLED": "true",
            "AGENTIT_RATE_LIMIT_DEFAULT_PER_MIN": "1",
        }):
            rate_limit._hits.clear()
            first = await client.get("/healthz")  # exempt path -- never counted
            assert first.status_code == 200
            r1 = await client.get("/ledger")
            r2 = await client.get("/ledger")
        assert r1.status_code != 429
        assert r2.status_code == 429
        assert r2.json()["detail"] == "Rate limit exceeded"

    async def test_disabled_by_default_never_429s(self, portal_client):
        client, _, _ = portal_client
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTIT_RATE_LIMIT_ENABLED", None)
            for _ in range(20):
                assert (await client.get("/ledger")).status_code != 429
