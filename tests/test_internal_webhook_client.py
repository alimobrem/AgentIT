"""Tests for the shared internal-webhook client helper.

Regression coverage for the root-cause bug shape this session hit twice:
a new cross-pod caller forgetting the ``X-Internal-Webhook-Token``
boilerplate entirely (``RemediationLoop``'s client shipped with no header
at all, confirmed live via repeated "loop-failed" events with "Missing or
invalid internal webhook token"). ``internal_webhook_client`` is now the
one place that attaches it, so this test asserts the header directly on
the client it returns -- the same assertion every caller's own
construction test now makes -- instead of on some particular caller's
call site.
"""

from __future__ import annotations

from agentit.internal_webhook_client import (
    INTERNAL_TOKEN_HEADER,
    internal_webhook_client,
    internal_webhook_headers,
)


class TestInternalWebhookHeaders:
    def test_includes_token_when_configured(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", "s3cr3t-token")
        assert internal_webhook_headers() == {INTERNAL_TOKEN_HEADER: "s3cr3t-token"}

    def test_empty_when_unset(self, monkeypatch):
        """Fails open, mirroring `verify_internal_token`'s own fail-open
        behavior on the receiving side (routes/webhooks.py) -- local dev/
        tests that never configure the secret must keep working."""
        monkeypatch.delenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", raising=False)
        assert internal_webhook_headers() == {}


class TestInternalWebhookClient:
    async def test_client_attaches_token_header_when_configured(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", "s3cr3t-token")
        client = internal_webhook_client(timeout=2)
        try:
            assert client.headers[INTERNAL_TOKEN_HEADER] == "s3cr3t-token"
        finally:
            await client.aclose()

    async def test_client_omits_token_header_when_unset(self, monkeypatch):
        monkeypatch.delenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", raising=False)
        client = internal_webhook_client(timeout=2)
        try:
            assert INTERNAL_TOKEN_HEADER not in client.headers
        finally:
            await client.aclose()

    async def test_passes_through_additional_kwargs(self, monkeypatch):
        monkeypatch.delenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", raising=False)
        client = internal_webhook_client(timeout=5, base_url="http://example.test")
        try:
            assert client.timeout.connect == 5
            assert str(client.base_url) == "http://example.test"
        finally:
            await client.aclose()

    async def test_caller_supplied_headers_are_preserved_alongside_token(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", "s3cr3t-token")
        client = internal_webhook_client(timeout=2, headers={"X-Custom": "value"})
        try:
            assert client.headers["X-Custom"] == "value"
            assert client.headers[INTERNAL_TOKEN_HEADER] == "s3cr3t-token"
        finally:
            await client.aclose()
