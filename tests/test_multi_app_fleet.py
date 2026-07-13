"""Multi-app fleet tests — verify fleet operations with multiple assessed apps."""
from __future__ import annotations
from unittest.mock import patch
import pytest
from conftest import make_report, make_store
from fastapi.testclient import TestClient
from agentit.portal.app import app
from agentit.portal.store_factory import AsyncSQLiteStore


@pytest.fixture()
def fleet_client():
    store = make_store()
    async_store = AsyncSQLiteStore.wrap(store)
    ids = []
    for name, url in [("frontend", "https://github.com/org/frontend"),
                       ("backend", "https://github.com/org/backend"),
                       ("worker", "https://github.com/org/worker")]:
        report = make_report()
        report.repo_url = url
        report.repo_name = name
        ids.append(store.save(report))

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.helpers.get_store", return_value=async_store), \
         patch("agentit.portal.helpers._store", async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store):
        yield TestClient(app), store, ids


class TestFleetOperations:
    def test_fleet_returns_all_apps(self, fleet_client):
        client, _, _ = fleet_client
        data = client.get("/api/fleet").json()
        assert len(data) == 3
        names = {a["repo_name"] for a in data}
        assert names == {"frontend", "backend", "worker"}

    def test_fleet_page_renders_all(self, fleet_client):
        client, _, _ = fleet_client
        text = client.get("/").text
        assert "frontend" in text
        assert "backend" in text
        assert "worker" in text

    def test_fleet_sorted_by_score(self, fleet_client):
        client, _, _ = fleet_client
        data = client.get("/api/fleet").json()
        scores = [a["latest_score"] for a in data]
        assert scores == sorted(scores)

    def test_individual_assessments_accessible(self, fleet_client):
        client, _, ids = fleet_client
        for aid in ids:
            assert client.get(f"/assessments/{aid}").status_code == 200

    def test_trend_with_reassessment(self, fleet_client):
        _, store, _ = fleet_client
        report2 = make_report()
        report2.repo_url = "https://github.com/org/frontend"
        report2.repo_name = "frontend"
        for s in report2.scores:
            s.score = min(100, s.score + 10)
        report2.model_post_init(None)
        store.save(report2)
        trend = store.get_trend("https://github.com/org/frontend")
        assert trend["assessments_count"] == 2
        assert trend["delta"] is not None
