"""Webhook security tests — signature verification, dedup, input validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from unittest.mock import MagicMock, patch
import pytest
from conftest import make_report


def _fake_request(headers: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = headers or {}
    return request


class TestGetDeliveryIdFallback:
    """Unit tests for `_get_delivery_id`'s no-header content-hash fallback --
    see its docstring in `webhooks.py` for the real bug this time-bucketing
    fixes (Tekton's `register-self-in-fleet` step and the `reassess-scheduler`
    watcher both repeat an identical body on every real call, with no
    delivery-id header)."""

    def test_prefers_the_github_delivery_header_when_present(self):
        from agentit.portal.routes.webhooks import _get_delivery_id

        request = _fake_request({"X-GitHub-Delivery": "abc-123"})
        assert _get_delivery_id(request, {"anything": "here"}) == "abc-123"

    def test_same_body_in_the_same_time_bucket_yields_the_same_id(self):
        from agentit.portal.routes.webhooks import _get_delivery_id

        request = _fake_request()
        body = {"repo_url": "https://github.com/t/r", "criticality": "high"}
        with patch("agentit.portal.routes.webhooks.time.time", return_value=1_000_000.0):
            first = _get_delivery_id(request, body)
            second = _get_delivery_id(request, body)
        assert first == second

    def test_same_body_in_a_later_time_bucket_yields_a_different_id(self):
        """The actual fix: without this, the Tekton `register-self-in-fleet`
        step's identical {repo_url, criticality} body on every CI run (and
        every same-app/criticality `reassess-scheduler` retrigger) would
        collide forever within the dedup retention window."""
        from agentit.portal.routes.webhooks import _DEDUP_TIME_BUCKET_SECONDS, _get_delivery_id

        request = _fake_request()
        body = {"repo_url": "https://github.com/t/r", "criticality": "high"}
        with patch("agentit.portal.routes.webhooks.time.time", return_value=1_000_000.0):
            first = _get_delivery_id(request, body)
        with patch(
            "agentit.portal.routes.webhooks.time.time",
            return_value=1_000_000.0 + _DEDUP_TIME_BUCKET_SECONDS * 3,
        ):
            second = _get_delivery_id(request, body)
        assert first != second

    def test_different_bodies_in_the_same_time_bucket_still_differ(self):
        from agentit.portal.routes.webhooks import _get_delivery_id

        request = _fake_request()
        with patch("agentit.portal.routes.webhooks.time.time", return_value=1_000_000.0):
            first = _get_delivery_id(request, {"repo_url": "https://github.com/t/a"})
            second = _get_delivery_id(request, {"repo_url": "https://github.com/t/b"})
        assert first != second


class TestGitHubSignature:
    def _sign(self, body: bytes, secret: str) -> str:
        return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"})
    async def test_valid_signature(self, portal_client):
        client, _, _ = portal_client
        body = json.dumps({"ref": "refs/heads/main", "repository": {"html_url": "https://github.com/t/r", "default_branch": "main"}}).encode()
        resp = await client.post("/api/webhook/github-push", content=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": self._sign(body, "test-secret"), "Content-Type": "application/json"})
        assert resp.status_code != 403

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"})
    async def test_invalid_signature_rejected(self, portal_client):
        client, _, _ = portal_client
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        resp = await client.post("/api/webhook/github-push", content=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=bad", "Content-Type": "application/json"})
        assert resp.status_code == 403

    @patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"})
    async def test_missing_signature_rejected(self, portal_client):
        client, _, _ = portal_client
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        resp = await client.post("/api/webhook/github-push", content=body,
            headers={"X-GitHub-Event": "push", "Content-Type": "application/json"})
        assert resp.status_code == 403

    async def test_no_secret_skips_verification(self, portal_client):
        client, _, _ = portal_client
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
            body = json.dumps({"ref": "refs/heads/main", "repository": {"html_url": "https://github.com/t/r", "default_branch": "main"}}).encode()
            resp = await client.post("/api/webhook/github-push", content=body,
                headers={"X-GitHub-Event": "push", "Content-Type": "application/json"})
        assert resp.status_code != 403


class TestInternalWebhookToken:
    """Part 3: shared-secret bearer-token auth for the Argo-Events-Sensor-only
    routes (verify_internal_token in routes/webhooks.py). Unlike the GitHub
    HMAC signature above, this covers /api/webhook/{assess,onboard,
    auto-apply,finding,remediate} -- never github-push, which keeps its own
    mechanism."""

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_missing_token_rejected(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/finding", json={"app_name": "test"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_invalid_token_rejected(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post(
            "/api/webhook/finding", json={"app_name": "test"},
            headers={"X-Internal-Webhook-Token": "wrong-token"},
        )
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_valid_token_passes_through_to_route_logic(self, portal_client):
        client, _, _ = portal_client
        # category is intentionally omitted -- proves the request got *past*
        # auth and into the route's own 400 validation, not blocked at 401.
        resp = await client.post(
            "/api/webhook/finding", json={"app_name": "test"},
            headers={"X-Internal-Webhook-Token": "s3cr3t-token"},
        )
        assert resp.status_code == 400

    async def test_no_token_configured_skips_verification(self, portal_client):
        """Matches the existing GITHUB_WEBHOOK_SECRET convention: fails open
        in local dev/tests where the secret was never configured. Every
        deployment templates this Secret (see chart/templates/
        internal-webhook-token-secret.yaml), so this path shouldn't be hit
        in a real cluster."""
        client, _, _ = portal_client
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTIT_INTERNAL_WEBHOOK_TOKEN", None)
            resp = await client.post("/api/webhook/finding", json={"app_name": "test"})
        assert resp.status_code == 400

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_onboard_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/onboard", json={"eventId": "evt-1"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_assess_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/assess", json={"repo_url": "https://github.com/t/r"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_skill_draft_requires_token(self, portal_client):
        """/api/webhook/skill-draft (the skill-learner watcher's cross-pod
        visibility fix) follows the same in-cluster-only convention as every
        other /api/webhook/* route above."""
        client, _, _ = portal_client
        resp = await client.post(
            "/api/webhook/skill-draft",
            json={"content": "---\nname: x\n---\nbody", "domain": "security"},
        )
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_skill_draft_valid_token_passes_through_to_route_logic(self, portal_client, tmp_path, monkeypatch):
        client, _, _ = portal_client
        monkeypatch.chdir(tmp_path)
        (tmp_path / "skills").mkdir()
        resp = await client.post(
            "/api/webhook/skill-draft",
            json={"content": "---\nname: token-check\ndomain: security\n---\nbody", "domain": "security"},
            headers={"X-Internal-Webhook-Token": "s3cr3t-token"},
        )
        assert resp.status_code == 200

    def test_skill_draft_route_registered_with_internal_token_dependency(self):
        """Regression guard for the 2026-07-15 incident: the skill-learner
        watcher's cross-pod webhook (`/api/webhook/skill-draft`) 404'd
        against a live pod. Root cause turned out to be Argo Rollouts
        version skew (the stable Service the watcher calls stayed pinned to
        an old ReplicaSet mid-canary-rollout), not a code regression -- but
        this asserts directly against `app.routes` that the route itself
        stays exactly where the watcher expects it (exact path, POST-only,
        gated by `verify_internal_token`) so a *future* accidental drop/
        rename/shadow during refactors (this session's webhooks.py/
        delivery.py/assessments.py churn, or any later one) fails a test
        instead of silently reappearing as a live-cluster incident.
        """
        from agentit.portal.app import app
        from agentit.portal.routes.webhooks import router as webhooks_router, verify_internal_token

        # OpenAPI schema reflects exactly what's actually wired into the
        # running `app` (survives however a given FastAPI/Starlette version
        # internally represents `include_router`'d routes) -- proves this
        # exact path is registered with a POST operation, i.e. not dropped
        # or shadowed by another route during app.py's route-module wiring.
        schema = app.openapi()
        assert "/api/webhook/skill-draft" in schema["paths"], "route missing, renamed, or not registered on app"
        assert "post" in schema["paths"]["/api/webhook/skill-draft"]

        # The route *definition* itself (in webhooks.py, independent of how
        # app.py wires it in) must still require verify_internal_token.
        matches = [r for r in webhooks_router.routes if getattr(r, "path", None) == "/api/webhook/skill-draft"]
        assert len(matches) == 1, "route missing, renamed, or duplicated in webhooks_router"
        route = matches[0]
        assert route.methods == {"POST"}
        dependency_calls = [d.call for d in route.dependant.dependencies]
        assert verify_internal_token in dependency_calls

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_skill_draft_valid_authed_request_never_404s(self, portal_client, tmp_path, monkeypatch):
        """A validly-authed request must never come back 404 -- that's
        exactly the symptom the watcher saw when it hit a stale pod during
        an in-flight canary rollout (see module docstring above). This is
        the code-level half of that guarantee: as long as this route is
        wired into the running app, an authed request resolves it."""
        client, _, _ = portal_client
        monkeypatch.chdir(tmp_path)
        (tmp_path / "skills").mkdir()
        resp = await client.post(
            "/api/webhook/skill-draft",
            json={"content": "---\nname: never-404\n---\nbody", "domain": "security"},
            headers={"X-Internal-Webhook-Token": "s3cr3t-token"},
        )
        assert resp.status_code != 404

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_github_push_route_unaffected_by_internal_token(self, portal_client):
        """github-push keeps its own HMAC-signature mechanism (TestGitHubSignature
        above) -- it must not also require the internal webhook token."""
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/github-push", json={}, headers={"X-GitHub-Event": "ping"})
        assert resp.status_code == 200

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_synthetic_probe_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/synthetic-probe", json={"up": True})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_backup_status_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/backup-status", json={"target": "sqlite", "status": "ok"})
        assert resp.status_code == 401

    @patch.dict(os.environ, {"AGENTIT_INTERNAL_WEBHOOK_TOKEN": "s3cr3t-token"})
    async def test_secret_check_requires_token(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/secret-check", json={"secret": "github-webhook-secret", "exists": False})
        assert resp.status_code == 401


class TestSelfMonitoringWebhooks:
    """/api/webhook/{synthetic-probe,backup-status,secret-check} -- see
    docs/deployment.md's 2026-07-13 incident writeup for why these three
    specific gaps (external uptime, backup verification, secret drift) were
    picked first."""

    async def test_synthetic_probe_up_sets_gauge(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/synthetic-probe", json={"up": True, "latency_ms": 42, "cert_days_remaining": 55})
        assert resp.status_code == 200
        from agentit.portal.metrics import synthetic_probe_up, route_cert_expiry_days
        assert synthetic_probe_up._value.get() == 1
        assert route_cert_expiry_days._value.get() == 55

    async def test_synthetic_probe_down_sets_gauge_and_logs_event(self, portal_client):
        client, store, _ = portal_client
        resp = await client.post("/api/webhook/synthetic-probe", json={"up": False, "detail": "http_code=503"})
        assert resp.status_code == 200
        from agentit.portal.metrics import synthetic_probe_up
        assert synthetic_probe_up._value.get() == 0
        events = await store.list_events(limit=5)
        assert any(e["action"] == "probe-failed" for e in events)

    async def test_backup_status_ok_sets_gauge(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/backup-status", json={"target": "sqlite", "status": "ok", "detail": "done"})
        assert resp.status_code == 200
        from agentit.portal.metrics import backup_last_status
        assert backup_last_status.labels(target="sqlite")._value.get() == 1

    async def test_backup_status_fail_sets_gauge_and_logs_warning(self, portal_client):
        client, store, _ = portal_client
        resp = await client.post("/api/webhook/backup-status", json={"target": "postgres", "status": "fail", "detail": "pg_dump exit 1"})
        assert resp.status_code == 200
        from agentit.portal.metrics import backup_last_status
        assert backup_last_status.labels(target="postgres")._value.get() == 0
        events = await store.list_events(limit=5)
        assert any(e["action"] == "backup-failed" for e in events)

    async def test_secret_check_missing_logs_critical_event(self, portal_client):
        client, store, _ = portal_client
        resp = await client.post("/api/webhook/secret-check", json={"secret": "github-webhook-secret", "exists": False})
        assert resp.status_code == 200
        from agentit.portal.metrics import secret_check_status
        assert secret_check_status.labels(secret="github-webhook-secret")._value.get() == 0
        events = await store.list_events(limit=5)
        assert any(e["action"] == "secret-missing" and e["severity"] == "critical" for e in events)

    async def test_secret_check_present_sets_gauge(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/secret-check", json={"secret": "github-webhook-secret", "exists": True})
        assert resp.status_code == 200
        from agentit.portal.metrics import secret_check_status
        assert secret_check_status.labels(secret="github-webhook-secret")._value.get() == 1


class TestWebhookDedup:
    @patch("agentit.portal.routes.webhooks.clone_assess_cleanup")
    async def test_duplicate_rejected(self, mock_assess, portal_client):
        client, _, _ = portal_client
        mock_assess.return_value = make_report()
        payload = {"repo_url": "https://github.com/t/dedup", "criticality": "low"}
        resp1 = await client.post("/api/webhook/assess", json=payload)
        assert resp1.status_code == 200
        resp2 = await client.post("/api/webhook/assess", json=payload)
        assert resp2.json().get("status") == "duplicate"

    @patch("agentit.portal.routes.webhooks.clone_assess_cleanup")
    async def test_distinct_calls_outside_the_time_bucket_are_not_treated_as_duplicates(
        self, mock_assess, portal_client,
    ):
        """Regression test for the content-hash-fallback edge case: with no
        delivery-id header, two genuinely separate calls that happen to
        share an identical body (the normal case for both
        `chart/templates/tekton/pipeline.yaml`'s `register-self-in-fleet`
        step, which POSTs the exact same {repo_url, criticality} on every
        CI run, and the `reassess-scheduler` watcher, which does the same
        for every re-assessment of a given app) must each be processed, not
        silently collapsed into "duplicate" for the whole dedup retention
        window. `_get_delivery_id()`'s time-bucketed fallback should let a
        call from a later bucket through even with an identical body."""
        import time as _time_module

        mock_assess.return_value = make_report()
        client, _, _ = portal_client
        payload = {"repo_url": "https://github.com/t/repeat-trigger", "criticality": "high"}

        resp1 = await client.post("/api/webhook/assess", json=payload)
        assert resp1.status_code == 200
        assert "assessment_id" in resp1.json()

        # Simulate the second call landing a full dedup-time-bucket later
        # (e.g. the next day's CI run, or the next remediation trigger for
        # the same app) -- still no delivery-id header, still an identical
        # body.
        from agentit.portal.routes import webhooks as webhooks_module

        with patch.object(
            webhooks_module.time, "time",
            return_value=_time_module.time() + webhooks_module._DEDUP_TIME_BUCKET_SECONDS * 2,
        ):
            resp2 = await client.post("/api/webhook/assess", json=payload)
        assert resp2.status_code == 200
        assert "assessment_id" in resp2.json()
        assert resp2.json().get("status") != "duplicate"
        assert mock_assess.call_count == 2

    @patch("agentit.portal.routes.webhooks.clone_assess_cleanup")
    async def test_concurrent_identical_deliveries_run_pipeline_once(self, mock_assess, portal_client):
        """Regression guard for the check-then-act webhook dedup race
        (Priority 1a): two genuinely concurrent, identical deliveries must
        not both execute the full (slow) assessment pipeline. Before the
        fix, `webhook_already_processed()` (a SELECT) ran up front but
        `mark_webhook_processed()` (the INSERT) only ran after the whole
        pipeline finished, so two near-simultaneous requests could both
        pass the SELECT before either INSERT landed. `clone_assess_cleanup`
        sleeps briefly here to widen that race window the way the real,
        multi-second clone-and-scan pipeline would.
        """
        import asyncio
        import time

        def _slow_assess(*args, **kwargs):
            time.sleep(0.3)
            return make_report()

        mock_assess.side_effect = _slow_assess
        client, _, _ = portal_client
        payload = {"repo_url": "https://github.com/t/concurrent-dedup", "criticality": "low"}

        resp1, resp2 = await asyncio.gather(
            client.post("/api/webhook/assess", json=payload),
            client.post("/api/webhook/assess", json=payload),
        )

        assert mock_assess.call_count == 1
        results = [resp1.json(), resp2.json()]
        duplicates = [r for r in results if r.get("status") == "duplicate"]
        completed = [r for r in results if "assessment_id" in r]
        assert len(duplicates) == 1
        assert len(completed) == 1


class TestWebhookValidation:
    async def test_assess_missing_repo_url(self, portal_client):
        client, _, _ = portal_client
        assert (await client.post("/api/webhook/assess", json={"criticality": "high"})).status_code == 400

    async def test_finding_missing_category(self, portal_client):
        client, _, _ = portal_client
        assert (await client.post("/api/webhook/finding", json={"app_name": "test"})).status_code == 400

    async def test_ping_returns_pong(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/github-push", json={}, headers={"X-GitHub-Event": "ping"})
        assert resp.json()["status"] == "pong"

    async def test_non_push_event_ignored(self, portal_client):
        client, _, _ = portal_client
        resp = await client.post("/api/webhook/github-push", json={}, headers={"X-GitHub-Event": "issues"})
        assert resp.json()["status"] == "ignored"
