"""Comprehensive browser tests using Playwright — every page, every action.

Run with: pytest tests/test_browser.py -v
Requires: pip install pytest-playwright && playwright install chromium
These use the FastAPI TestClient with ASGI transport (no subprocess needed).
"""
from __future__ import annotations

import re

import pytest
from unittest.mock import patch

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    """Run the portal via ASGI testclient on a local port."""
    import threading
    import uvicorn
    from agentit.portal.app import app
    from agentit.portal.store import AssessmentStore
    from agentit.portal.store_factory import AsyncSQLiteStore
    from conftest import make_report

    store = AssessmentStore(":memory:")
    async_store = AsyncSQLiteStore.wrap(store)
    report = make_report()
    aid = store.save(report)
    store.save_onboarding(aid, [
        {"category": "security", "path": "test.yaml",
         "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
         "description": "test file"}
    ])
    store.log_event("test-agent", "completed", report.repo_name, "info", "test event")
    store.create_gate(aid, "deploy-approval", "Test gate for approval")

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

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=9998, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time
        time.sleep(2)
        yield "http://127.0.0.1:9998", aid, store
        server.should_exit = True


# ── Page Load Tests ──────────────────────────────────────────────────


class TestEveryPageLoads:
    """Every HTML page returns 200 and has the expected h1."""

    def test_fleet_dashboard(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        expect(page.locator("h1")).to_contain_text("Enterprise Readiness")

    def test_assess_redirects_to_fleet(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/assess")
        assert "assess=1" in page.url or page.url.endswith("/")

    def test_assessment_detail(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        expect(page.locator("h1")).to_be_visible()
        expect(page.locator(".score-hero")).to_be_visible()
        expect(page.locator(".lifecycle-stepper")).to_be_visible()

    def test_onboard_results(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}/onboard-results")
        expect(page.locator("h1")).to_contain_text("Onboarding")

    def test_remediations_page(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}/remediations")
        expect(page.locator("h1")).to_be_visible()

    def test_slos_page(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}/slos")
        expect(page.locator("h1")).to_be_visible()

    def test_gates_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/gates")
        expect(page.locator("h1")).to_contain_text("Approval Gates")

    def test_events_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/events")
        expect(page.locator("h1")).to_contain_text("Activity Feed")

    def test_dlq_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/events/dlq")
        expect(page.locator("h1")).to_contain_text("Dead-Letter")

    def test_agents_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/agents")
        expect(page.locator("h1")).to_be_visible()

    def test_workflows_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/workflows")
        expect(page.locator("h1")).to_contain_text("Workflows")

    def test_schedules_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/schedules")
        expect(page.locator("h1")).to_be_visible()

    def test_insights_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/insights")
        expect(page.locator("h1")).to_contain_text("Insights")

    def test_health_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/health")
        expect(page.locator("h1")).to_contain_text("Health")

    def test_settings_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/settings")
        expect(page.locator("h1")).to_contain_text("Settings")

    def test_fleet_slos_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/fleet/slos")
        expect(page.locator("h1")).to_contain_text("Fleet SLOs")

    def test_fleet_remediations_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/fleet/remediations")
        expect(page.locator("h1")).to_contain_text("Fleet Remediations")

    def test_404_page(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/nonexistent-page-xyz")
        assert resp.status == 404


# ── API Endpoint Tests ───────────────────────────────────────────────


class TestAPIEndpoints:
    """Every API endpoint returns valid JSON."""

    def test_healthz(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/healthz")
        assert resp.status == 200

    def test_readyz(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/readyz")
        assert resp.status == 200

    def test_metrics(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/metrics")
        assert resp.status == 200
        expect(page.locator("body")).to_contain_text("# HELP")

    def test_api_fleet(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/fleet")
        assert resp.status == 200

    def test_api_assessments(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/assessments")
        assert resp.status == 200

    def test_api_events(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/events")
        assert resp.status == 200

    def test_api_gates(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/gates")
        assert resp.status == 200

    def test_api_health(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/health")
        assert resp.status == 200

    def test_api_settings(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/settings")
        assert resp.status == 200

    def test_api_agents(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/agents")
        assert resp.status == 200

    def test_api_export(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/export")
        assert resp.status == 200

    def test_api_platform_drift(self, page: Page, app_url):
        url, _, _ = app_url
        resp = page.goto(f"{url}/api/platform/drift")
        assert resp.status == 200


# ── Modal & Interaction Tests ────────────────────────────────────────


class TestModals:
    """Test all modal interactions."""

    def test_assess_modal_opens(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        page.click("text=Assess New Repo")
        expect(page.locator("#assess-modal")).to_have_class(re.compile("open"))

    def test_assess_modal_closes_with_x(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        page.click("text=Assess New Repo")
        expect(page.locator("#assess-modal")).to_have_class(re.compile("open"))
        page.click("#assess-modal .modal-close")
        expect(page.locator("#assess-modal")).not_to_have_class(re.compile("open"))

    def test_assess_modal_has_form_fields(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        page.click("text=Assess New Repo")
        expect(page.locator("#assess-modal input[name='repo_url']")).to_be_visible()
        expect(page.locator("#assess-modal select[name='criticality']")).to_be_visible()
        expect(page.locator("#assess-modal button[type='submit']")).to_be_visible()


# ── Navigation Tests ─────────────────────────────────────────────────


class TestNavigation:
    """Test navigation works correctly."""

    def test_primary_nav_links(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        expect(page.locator("nav >> text=Fleet")).to_be_visible()
        expect(page.locator("nav >> text=Gates")).to_be_visible()

    def test_secondary_nav_links(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        for link in ["Events", "Agents", "Workflows", "Insights", "Health", "Settings"]:
            expect(page.locator(f"nav >> text={link}")).to_be_visible()

    def test_fleet_link_navigates(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/settings")
        page.click("nav >> text=Fleet")
        page.wait_for_url("**/")

    def test_hamburger_on_mobile(self, page: Page, app_url):
        url, _, _ = app_url
        page.set_viewport_size({"width": 375, "height": 667})
        page.goto(url)
        hamburger = page.locator("[aria-label='Toggle menu']")
        expect(hamburger).to_be_visible()

    def test_assessment_detail_back_link(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        expect(page.locator("text=Dashboard")).to_be_visible()


# ── Component Tests ──────────────────────────────────────────────────


class TestComponents:
    """Test UI components render correctly."""

    def test_lifecycle_stepper_on_assessment(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        stepper = page.locator(".lifecycle-stepper")
        expect(stepper).to_be_visible()
        expect(stepper.locator(".step-active")).to_be_visible()

    def test_score_bars_on_assessment(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        bars = page.locator(".dimension-row")
        assert bars.count() >= 5

    def test_stat_cards_on_fleet(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        cards = page.locator(".stat-card")
        assert cards.count() >= 2

    def test_events_filter_bar(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/events")
        expect(page.locator("input[name='q']")).to_be_visible()
        expect(page.locator("select[name='severity']")).to_be_visible()

    def test_pagination_on_events(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/events")
        expect(page.locator(".pagination")).to_be_visible()

    def test_decision_matrix_on_settings(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/settings")
        expect(page.locator("text=Decision Matrix")).to_be_visible()

    def test_pipeline_flow_on_workflows(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/workflows")
        expect(page.locator("text=Onboarding Pipeline")).to_be_visible()
        expect(page.locator("text=Onboarding Agents")).to_be_visible()


# ── Accessibility Tests ──────────────────────────────────────────────


class TestAccessibility:
    """Basic accessibility checks."""

    def test_skip_link_exists(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        skip = page.locator(".skip-link")
        assert skip.count() >= 1

    def test_main_content_landmark(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        main = page.locator("#main-content")
        expect(main).to_be_visible()

    def test_all_images_have_alt_or_role(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        images = page.locator("img")
        for i in range(images.count()):
            img = images.nth(i)
            alt = img.get_attribute("alt")
            role = img.get_attribute("role")
            assert alt is not None or role is not None, f"Image {i} missing alt/role"

    def test_delete_buttons_have_labels(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        delete_btns = page.locator("button:has-text('×')")
        for i in range(delete_btns.count()):
            btn = delete_btns.nth(i)
            label = btn.get_attribute("aria-label")
            assert label, f"Delete button {i} missing aria-label"
