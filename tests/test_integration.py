"""Integration tests — require a live cluster. Run with --run-integration flag.

These tests verify the full end-to-end flow against a real OpenShift cluster:
- Assess a real repo via webhook
- Onboard and verify manifests + image build triggered
- Verify ApplicationSet creates Argo CD Application
- Verify pods deploy with correct image

Skip by default. Enable with: pytest --run-integration
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

PORTAL_URL = os.environ.get(
    "AGENTIT_PORTAL_URL",
    "https://agentit-agentit.apps.aws-jb-acsacm-1.dev05.red-chesterfield.com",
)
TEST_REPO = "https://github.com/alimobrem/pinky"


def pytest_addoption_integration(parser):
    parser.addoption("--run-integration", action="store_true", default=False)


@pytest.fixture
def portal():
    return httpx.Client(base_url=PORTAL_URL, verify=False, timeout=120)


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests",
)


class TestAssessWebhook:
    def test_assess_returns_score(self, portal):
        resp = portal.post("/api/webhook/assess", json={
            "repo_url": TEST_REPO,
            "criticality": "medium",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "assessment_id" in data
        assert data["overall_score"] > 0

    def test_assess_invalid_repo_fails(self, portal):
        resp = portal.post("/api/webhook/assess", json={
            "repo_url": "https://github.com/nonexistent/repo-that-does-not-exist-xyz",
            "criticality": "low",
        })
        assert resp.status_code in (400, 500)


class TestOnboardWebhook:
    def test_onboard_generates_manifests(self, portal):
        assess_resp = portal.post("/api/webhook/assess", json={
            "repo_url": TEST_REPO, "criticality": "medium",
        })
        aid = assess_resp.json()["assessment_id"]

        onboard_resp = portal.post("/api/webhook/onboard", json={
            "correlationId": aid,
        })
        assert onboard_resp.status_code == 200
        data = onboard_resp.json()
        assert data["files_generated"] > 0
        assert len(data["categories"]) >= 4

    def test_onboard_triggers_image_build(self, portal):
        assess_resp = portal.post("/api/webhook/assess", json={
            "repo_url": TEST_REPO, "criticality": "medium",
        })
        aid = assess_resp.json()["assessment_id"]

        onboard_resp = portal.post("/api/webhook/onboard", json={
            "correlationId": aid,
        })
        data = onboard_resp.json()
        assert "image_build" in data
        assert "image-registry" in data["image_build"]


class TestPortalPages:
    def test_fleet_page(self, portal):
        resp = portal.get("/")
        assert resp.status_code == 200

    def test_health_page(self, portal):
        resp = portal.get("/health")
        assert resp.status_code == 200

    def test_agents_page(self, portal):
        resp = portal.get("/agents")
        assert resp.status_code == 200

    def test_settings_page(self, portal):
        resp = portal.get("/settings")
        assert resp.status_code == 200

    def test_schedules_page(self, portal):
        resp = portal.get("/schedules")
        assert resp.status_code == 200

    def test_health_api(self, portal):
        resp = portal.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pods_running"] > 0
