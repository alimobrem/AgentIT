"""routes/recommendations.py -- the real, direct actions that replaced the
generic gates system for rollback-review (SLO breach -> rollback decision)
and finding-unresolved-escalation (Phase 4's bounded-retry stop condition)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


@pytest.fixture
async def rec_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.recommendations.get_store", return_value=store):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True,
        ) as client:
            await prime_csrf(client)
            yield client, store


class TestRollbackExecute:
    async def test_execute_calls_real_rollback_action_and_marks_resolved(self, rec_client):
        client, store = rec_client
        aid = await store.save(make_report(repo_name="rollback-app"))
        await store.save_apply_results(
            aid, {"applied": ["deployment.yaml"], "skipped": [], "errors": []}, "rollback-app", dry_run=False,
        )
        event_id = await store.log_event(
            "slo-tracker", "rollback-recommended", "rollback-app", "critical", "SLO breach",
        )

        with patch("agentit.remediation_loop.rollback_action") as mock_rollback:
            mock_rollback.return_value = {"outcome": "rolled_back", "details": "Argo Rollout aborted"}
            resp = await client.post(
                f"/rollback/{event_id}/execute", data={"assessment_id": aid}, follow_redirects=False,
            )

        mock_rollback.assert_called_once_with("rollback-app", "rollback-app")
        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]

        unresolved = await store.list_unresolved_events(
            "rollback-recommended", ["rollback-executed", "rollback-dismissed"],
        )
        assert unresolved == []
        events = await store.list_events()
        assert any(e["action"] == "rollback-executed" for e in events)

    async def test_failed_rollback_leaves_it_unresolved_and_shows_error(self, rec_client):
        client, store = rec_client
        aid = await store.save(make_report(repo_name="rollback-fail-app"))
        event_id = await store.log_event(
            "slo-tracker", "rollback-recommended", "rollback-fail-app", "critical", "SLO breach",
        )

        with patch("agentit.remediation_loop.rollback_action") as mock_rollback:
            mock_rollback.return_value = {"outcome": "rollback_failed", "error": "no Rollout found"}
            resp = await client.post(
                f"/rollback/{event_id}/execute", data={"assessment_id": aid}, follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        events = await store.list_events()
        rollback_executed = [e for e in events if e["action"] == "rollback-executed"]
        assert len(rollback_executed) == 1
        assert rollback_executed[0]["severity"] == "warning"

    async def test_unknown_event_id_404s(self, rec_client):
        client, _store = rec_client
        resp = await client.post("/rollback/nonexistent/execute", data={}, follow_redirects=False)
        assert resp.status_code == 404

    async def test_wrong_action_type_event_id_404s(self, rec_client):
        """A finding-escalated event id must never be resolvable via the
        rollback route -- these are two distinct recommendation types."""
        client, store = rec_client
        event_id = await store.log_event(
            "delivery-verifier", "finding-escalated", "some-app", "critical", "escalated",
        )
        resp = await client.post(f"/rollback/{event_id}/execute", data={}, follow_redirects=False)
        assert resp.status_code == 404


class TestRollbackDismiss:
    async def test_dismiss_resolves_without_performing_a_rollback(self, rec_client):
        client, store = rec_client
        event_id = await store.log_event(
            "slo-tracker", "rollback-recommended", "dismiss-app", "critical", "SLO breach",
        )
        with patch("agentit.remediation_loop.rollback_action") as mock_rollback:
            resp = await client.post(f"/rollback/{event_id}/dismiss", data={}, follow_redirects=False)
        mock_rollback.assert_not_called()
        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]

        unresolved = await store.list_unresolved_events(
            "rollback-recommended", ["rollback-executed", "rollback-dismissed"],
        )
        assert unresolved == []


class TestFindingEscalationAcknowledge:
    async def test_acknowledge_resolves_without_redelivery(self, rec_client):
        client, store = rec_client
        event_id = await store.log_event(
            "delivery-verifier", "finding-escalated", "escalated-app", "critical",
            "'security' finding has failed to resolve after 3 automated fix attempt(s)",
        )
        resp = await client.post(f"/findings/{event_id}/acknowledge", data={}, follow_redirects=False)
        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]

        unresolved = await store.list_unresolved_events(
            "finding-escalated", ["finding-escalation-acknowledged"],
        )
        assert unresolved == []
        events = await store.list_events()
        assert any(e["action"] == "finding-escalation-acknowledged" for e in events)

    async def test_unknown_event_id_404s(self, rec_client):
        client, _store = rec_client
        resp = await client.post("/findings/nonexistent/acknowledge", data={}, follow_redirects=False)
        assert resp.status_code == 404
