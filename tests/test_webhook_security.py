"""Webhook security tests — signature verification, dedup, input validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from unittest.mock import patch
import pytest
from conftest import make_report


class TestGitHubSignature:
    def _sign(self, body: bytes, secret: str) -> str:
        return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"})
    def test_valid_signature(self, portal_client):
        client, _, _ = portal_client
        body = json.dumps({"ref": "refs/heads/main", "repository": {"html_url": "https://github.com/t/r", "default_branch": "main"}}).encode()
        resp = client.post("/api/webhook/github-push", content=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": self._sign(body, "test-secret"), "Content-Type": "application/json"})
        assert resp.status_code != 403

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"})
    def test_invalid_signature_rejected(self, portal_client):
        client, _, _ = portal_client
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        resp = client.post("/api/webhook/github-push", content=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=bad", "Content-Type": "application/json"})
        assert resp.status_code == 403

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"})
    def test_missing_signature_rejected(self, portal_client):
        client, _, _ = portal_client
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        resp = client.post("/api/webhook/github-push", content=body,
            headers={"X-GitHub-Event": "push", "Content-Type": "application/json"})
        assert resp.status_code == 403

    def test_no_secret_skips_verification(self, portal_client):
        client, _, _ = portal_client
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
            body = json.dumps({"ref": "refs/heads/main", "repository": {"html_url": "https://github.com/t/r", "default_branch": "main"}}).encode()
            resp = client.post("/api/webhook/github-push", content=body,
                headers={"X-GitHub-Event": "push", "Content-Type": "application/json"})
        assert resp.status_code != 403


class TestWebhookDedup:
    @patch("agentit.portal.routes.webhooks.clone_assess_cleanup")
    def test_duplicate_rejected(self, mock_assess, portal_client):
        client, _, _ = portal_client
        mock_assess.return_value = make_report()
        payload = {"repo_url": "https://github.com/t/dedup", "criticality": "low"}
        resp1 = client.post("/api/webhook/assess", json=payload)
        assert resp1.status_code == 200
        resp2 = client.post("/api/webhook/assess", json=payload)
        assert resp2.json().get("status") == "duplicate"


class TestWebhookValidation:
    def test_assess_missing_repo_url(self, portal_client):
        client, _, _ = portal_client
        assert client.post("/api/webhook/assess", json={"criticality": "high"}).status_code == 400

    def test_finding_missing_category(self, portal_client):
        client, _, _ = portal_client
        assert client.post("/api/webhook/finding", json={"app_name": "test"}).status_code == 400

    def test_ping_returns_pong(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/github-push", json={}, headers={"X-GitHub-Event": "ping"})
        assert resp.json()["status"] == "pong"

    def test_non_push_event_ignored(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/github-push", json={}, headers={"X-GitHub-Event": "issues"})
        assert resp.json()["status"] == "ignored"
