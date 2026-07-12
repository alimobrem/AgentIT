"""Template rendering regression tests — every page renders with expected elements."""
from __future__ import annotations
import pytest


class TestTemplateRendering:
    def test_fleet_has_nav(self, portal_client):
        client, _, _ = portal_client
        text = client.get("/").text
        assert "AgentIT" in text

    def test_assess_form_has_inputs(self, portal_client):
        client, _, _ = portal_client
        text = client.get("/assess").text
        assert "<form" in text
        assert "repo" in text.lower()

    def test_assessment_detail_has_scores(self, portal_client):
        client, _, aid = portal_client
        text = client.get(f"/assessments/{aid}").text
        assert "security" in text.lower()
        assert "/100" in text

    def test_events_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/events").status_code == 200

    def test_gates_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/gates").status_code == 200

    def test_agents_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/agents").status_code == 200

    def test_settings_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/settings").status_code == 200

    def test_schedules_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/schedules").status_code == 200

    def test_workflows_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/workflows").status_code == 200

    def test_health_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/health").status_code == 200

    def test_dlq_page_renders(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/events/dlq").status_code == 200

    def test_onboard_results_renders(self, portal_client):
        client, _, aid = portal_client
        assert client.get(f"/assessments/{aid}/onboard-results").status_code == 200

    def test_remediations_renders(self, portal_client):
        client, _, aid = portal_client
        assert client.get(f"/assessments/{aid}/remediations").status_code == 200

    def test_slos_renders(self, portal_client):
        client, _, aid = portal_client
        assert client.get(f"/assessments/{aid}/slos").status_code == 200

    def test_404_page(self, portal_client):
        client, _, _ = portal_client
        assert client.get("/nonexistent-xyz").status_code == 404

    def test_all_pages_have_css(self, portal_client):
        client, _, aid = portal_client
        for page in ["/", "/assess", f"/assessments/{aid}", "/events", "/gates",
                     "/agents", "/settings", "/workflows", "/health"]:
            resp = client.get(page)
            assert resp.status_code == 200, f"{page} returned {resp.status_code}"
            assert "<style" in resp.text, f"{page} missing CSS"
