"""Multi-app fleet tests — verify fleet operations with multiple assessed apps."""
from __future__ import annotations
from unittest.mock import patch
import pytest
from conftest import make_report, make_store
from httpx import ASGITransport, AsyncClient
from agentit.portal.app import app


@pytest.fixture()
async def fleet_client():
    store = await make_store()
    async_store = store
    ids = []
    for name, url in [("frontend", "https://github.com/org/frontend"),
                       ("backend", "https://github.com/org/backend"),
                       ("worker", "https://github.com/org/worker")]:
        report = make_report()
        report.repo_url = url
        report.repo_name = name
        ids.append(await store.save(report))

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.helpers.get_store", return_value=async_store), \
         patch("agentit.portal.helpers._store", async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.gates.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.routes.remediations.get_store", return_value=async_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=async_store):
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True), store, ids


class TestFleetOperations:
    async def test_fleet_returns_all_apps(self, fleet_client):
        client, _, _ = fleet_client
        data = (await client.get("/api/fleet")).json()
        assert len(data) == 3
        names = {a["repo_name"] for a in data}
        assert names == {"frontend", "backend", "worker"}

    async def test_fleet_page_renders_all(self, fleet_client):
        client, _, _ = fleet_client
        text = (await client.get("/")).text
        assert "frontend" in text
        assert "backend" in text
        assert "worker" in text

    async def test_fleet_sorted_by_score(self, fleet_client):
        client, _, _ = fleet_client
        data = (await client.get("/api/fleet")).json()
        scores = [a["latest_score"] for a in data]
        assert scores == sorted(scores)

    async def test_individual_assessments_accessible(self, fleet_client):
        client, _, ids = fleet_client
        for aid in ids:
            assert (await client.get(f"/assessments/{aid}")).status_code == 200

    async def test_trend_with_reassessment(self, fleet_client):
        _, store, _ = fleet_client
        report2 = make_report()
        report2.repo_url = "https://github.com/org/frontend"
        report2.repo_name = "frontend"
        for s in report2.scores:
            s.score = min(100, s.score + 10)
        report2.model_post_init(None)
        await store.save(report2)
        trend = await store.get_trend("https://github.com/org/frontend")
        assert trend["assessments_count"] == 2
        assert trend["delta"] is not None

    async def test_needs_action_badge_survives_reassessment(self, fleet_client):
        """Orphaned-gate-attribution regression: a gate created against
        "frontend"'s OLD (pre-fixture) assessment must still show up as a
        "needs action" badge on the fleet page after "frontend" is
        re-assessed, since `_attach_pending_actions` (fleet.py) now keys off
        `repo_url` instead of the stale `assessment_id`.
        """
        client, store, ids = fleet_client
        frontend_id = None
        for aid in ids:
            report = await store.get(aid)
            if report.repo_url == "https://github.com/org/frontend":
                frontend_id = aid
                break
        await store.create_gate(frontend_id, "security", "needs review")

        report2 = make_report()
        report2.repo_url = "https://github.com/org/frontend"
        report2.repo_name = "frontend"
        report2.model_post_init(None)
        new_frontend_id = await store.save(report2)
        assert new_frontend_id != frontend_id

        text = (await client.get("/")).text
        assert f'/assessments/{new_frontend_id}?tab=actions' in text
        assert "1 pending" in text
