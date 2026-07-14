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


class TestInternalWebhookToken:
    """Part 3: shared-secret bearer-token auth for the Argo-Events-Sensor-only
    routes (verify_internal_token in routes/webhooks.py). Unlike the GitHub
    HMAC signature above, this covers /api/webhook/{assess,onboard,
    auto-apply,finding,remediate} -- never github-push, which keeps its own
    mechanism."""

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_missing_token_rejected(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/finding", json={"app_name": "test"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_invalid_token_rejected(self, portal_client):
        client, _, _ = portal_client
        resp = client.post(
            "/api/webhook/finding", json={"app_name": "test"},
            headers={"X-Internal-Webhook-Token": "wrong-token"},
        )
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_valid_token_passes_through_to_route_logic(self, portal_client):
        client, _, _ = portal_client
        # category is intentionally omitted -- proves the request got *past*
        # auth and into the route's own 400 validation, not blocked at 401.
        resp = client.post(
            "/api/webhook/finding", json={"app_name": "test"},
            headers={"X-Internal-Webhook-Token": "s3cr3t-token"},
        )
        assert resp.status_code == 400

    def test_no_token_configured_skips_verification(self, portal_client):
        """Matches the existing GITHUB_WEBHOOK_SECRET convention: fails open
        in local dev/tests where the secret was never configured. Every
        deployment templates this Secret (see chart/templates/
        internal-webhook-token-secret.yaml), so this path shouldn't be hit
        in a real cluster."""
        client, _, _ = portal_client
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTIT_INTERNAL_WEBHOOK_TOKEN", None)
            resp = client.post("/api/webhook/finding", json={"app_name": "test"})
        assert resp.status_code == 400

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_onboard_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/onboard", json={"eventId": "evt-1"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_auto_apply_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/auto-apply", json={"assessment_id": "x"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_remediate_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/remediate", json={"repo_url": "https://github.com/t/r"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_assess_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/assess", json={"repo_url": "https://github.com/t/r"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_skill_draft_requires_token(self, portal_client):
        """/api/webhook/skill-draft (the skill-learner watcher's cross-pod
        visibility fix) follows the same in-cluster-only convention as every
        other /api/webhook/* route above."""
        client, _, _ = portal_client
        resp = client.post(
            "/api/webhook/skill-draft",
            json={"content": "---\nname: x\n---\nbody", "domain": "security"},
        )
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_skill_draft_valid_token_passes_through_to_route_logic(self, portal_client, tmp_path, monkeypatch):
        client, _, _ = portal_client
        monkeypatch.chdir(tmp_path)
        (tmp_path / "skills").mkdir()
        resp = client.post(
            "/api/webhook/skill-draft",
            json={"content": "---\nname: token-check\ndomain: security\n---\nbody", "domain": "security"},
            headers={"X-Internal-Webhook-Token": "s3cr3t-token"},
        )
        assert resp.status_code == 200

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_github_push_route_unaffected_by_internal_token(self, portal_client):
        """github-push keeps its own HMAC-signature mechanism (TestGitHubSignature
        above) -- it must not also require the internal webhook token."""
        client, _, _ = portal_client
        resp = client.post("/api/webhook/github-push", json={}, headers={"X-GitHub-Event": "ping"})
        assert resp.status_code == 200

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_synthetic_probe_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/synthetic-probe", json={"up": True})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_backup_status_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/backup-status", json={"target": "sqlite", "status": "ok"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    def test_secret_check_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/secret-check", json={"secret": "github-webhook-secret", "exists": False})
        assert resp.status_code == 401


class TestSelfMonitoringWebhooks:
    """/api/webhook/{synthetic-probe,backup-status,secret-check} -- see
    docs/deployment.md's 2026-07-13 incident writeup for why these three
    specific gaps (external uptime, backup verification, secret drift) were
    picked first."""

    def test_synthetic_probe_up_sets_gauge(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/synthetic-probe", json={"up": True, "latency_ms": 42, "cert_days_remaining": 55})
        assert resp.status_code == 200
        from agentit.portal.metrics import synthetic_probe_up, route_cert_expiry_days
        assert synthetic_probe_up._value.get() == 1
        assert route_cert_expiry_days._value.get() == 55

    def test_synthetic_probe_down_sets_gauge_and_logs_event(self, portal_client):
        client, store, _ = portal_client
        resp = client.post("/api/webhook/synthetic-probe", json={"up": False, "detail": "http_code=503"})
        assert resp.status_code == 200
        from agentit.portal.metrics import synthetic_probe_up
        assert synthetic_probe_up._value.get() == 0
        events = store.list_events(limit=5)
        assert any(e["action"] == "probe-failed" for e in events)

    def test_backup_status_ok_sets_gauge(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/backup-status", json={"target": "sqlite", "status": "ok", "detail": "done"})
        assert resp.status_code == 200
        from agentit.portal.metrics import backup_last_status
        assert backup_last_status.labels(target="sqlite")._value.get() == 1

    def test_backup_status_fail_sets_gauge_and_logs_warning(self, portal_client):
        client, store, _ = portal_client
        resp = client.post("/api/webhook/backup-status", json={"target": "postgres", "status": "fail", "detail": "pg_dump exit 1"})
        assert resp.status_code == 200
        from agentit.portal.metrics import backup_last_status
        assert backup_last_status.labels(target="postgres")._value.get() == 0
        events = store.list_events(limit=5)
        assert any(e["action"] == "backup-failed" for e in events)

    def test_secret_check_missing_logs_critical_event(self, portal_client):
        client, store, _ = portal_client
        resp = client.post("/api/webhook/secret-check", json={"secret": "github-webhook-secret", "exists": False})
        assert resp.status_code == 200
        from agentit.portal.metrics import secret_check_status
        assert secret_check_status.labels(secret="github-webhook-secret")._value.get() == 0
        events = store.list_events(limit=5)
        assert any(e["action"] == "secret-missing" and e["severity"] == "critical" for e in events)

    def test_secret_check_present_sets_gauge(self, portal_client):
        client, _, _ = portal_client
        resp = client.post("/api/webhook/secret-check", json={"secret": "github-webhook-secret", "exists": True})
        assert resp.status_code == 200
        from agentit.portal.metrics import secret_check_status
        assert secret_check_status.labels(secret="github-webhook-secret")._value.get() == 1


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
