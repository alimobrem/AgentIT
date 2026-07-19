"""Performance tests — assert response times for all portal pages and APIs."""
from __future__ import annotations
import time
import pytest


FAST = 0.5
MEDIUM = 1.0


async def _timed_get(client, path):
    t0 = time.monotonic()
    resp = await client.get(path)
    return resp, time.monotonic() - t0


class TestPagePerformance:
    async def test_fleet_dashboard(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/")
        assert resp.status_code == 200
        assert t < MEDIUM, f"/ took {t:.2f}s"

    async def test_assess_form(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/assess")
        assert resp.status_code == 200
        assert t < FAST

    async def test_assessment_detail(self, portal_client):
        client, _, aid = portal_client
        resp, t = await _timed_get(client, f"/assessments/{aid}")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_events_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/events")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_gates_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/gates")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_agents_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/agents")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_settings_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/settings")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_schedules_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/schedules")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_workflows_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/workflows")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_health_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/health")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_dlq_page(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/events/dlq")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_slos_page(self, portal_client):
        client, _, aid = portal_client
        resp, t = await _timed_get(client, f"/assessments/{aid}/slos")
        assert resp.status_code == 200
        assert t < MEDIUM


class TestAPIPerformance:
    async def test_healthz(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/healthz")
        assert resp.status_code == 200
        assert t < 0.1, f"/healthz took {t:.2f}s"

    async def test_readyz(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/readyz")
        assert resp.status_code == 200
        assert t < 0.2

    async def test_api_fleet(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/api/fleet")
        assert resp.status_code == 200
        assert t < FAST

    async def test_api_events(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/api/events")
        assert resp.status_code == 200
        assert t < FAST

    async def test_api_assessments(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/api/assessments")
        assert resp.status_code == 200
        assert t < FAST

    async def test_api_health(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/api/health")
        assert resp.status_code == 200
        assert t < MEDIUM

    async def test_api_export(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/api/export")
        assert resp.status_code == 200
        assert t < FAST

    async def test_api_settings(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/api/settings")
        assert resp.status_code == 200
        assert t < FAST

    async def test_metrics(self, portal_client):
        client, _, _ = portal_client
        resp, t = await _timed_get(client, "/metrics")
        assert resp.status_code == 200
        assert t < FAST
