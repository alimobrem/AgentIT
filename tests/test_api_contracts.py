"""API contract tests — verify response shapes for all API endpoints."""
from __future__ import annotations
import pytest


class TestAPIContracts:
    async def test_fleet_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/fleet")).json()
        assert isinstance(data, list)
        if data:
            assert "id" in data[0]
            assert "repo_name" in data[0]
            assert "latest_score" in data[0]

    async def test_assessments_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/assessments")).json()
        assert isinstance(data, list)
        if data:
            assert "id" in data[0]
            assert "overall_score" in data[0]

    async def test_assessment_detail_shape(self, portal_client):
        client, _, aid = portal_client
        data = (await client.get(f"/api/assessments/{aid}")).json()
        assert "scores" in data
        assert "overall_score" in data
        assert "remediation_plan" in data

    async def test_events_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/events")).json()
        assert isinstance(data, list)
        if data:
            assert "agent_id" in data[0]
            assert "action" in data[0]

    async def test_gates_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/gates")).json()
        assert isinstance(data, list)

    async def test_health_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/health")).json()
        assert "pods_running" in data
        assert "kafka_ready" in data

    async def test_settings_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/settings")).json()
        assert isinstance(data, list)

    async def test_export_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/export")).json()
        for key in ("assessments", "events", "gates", "remediations", "slos"):
            assert key in data
            assert isinstance(data[key], list)

    async def test_agents_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/api/agents")).json()
        assert isinstance(data, list)

    async def test_healthz_shape(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/healthz")).json() == {"status": "ok"}

    async def test_readyz_shape(self, portal_client):
        client, _, _ = portal_client
        data = (await client.get("/readyz")).json()
        assert data["status"] in ("ready", "not ready")

    async def test_metrics_prometheus_format(self, portal_client):
        client, _, _ = portal_client
        text = (await client.get("/metrics")).text
        assert "# HELP" in text
        assert "# TYPE" in text

    async def test_manifests_shape(self, portal_client):
        client, _, aid = portal_client
        data = (await client.get(f"/api/assessments/{aid}/manifests")).json()
        assert isinstance(data, list)

    async def test_nonexistent_assessment_404(self, portal_client):
        client, _, _ = portal_client
        assert (await client.get("/api/assessments/nonexistent")).status_code == 404
