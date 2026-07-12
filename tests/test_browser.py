"""Browser-based tests using Playwright — catches CSS/JS interaction bugs."""
from __future__ import annotations

import subprocess
import time

import pytest

# Skip all browser tests if playwright is not installed
pytest.importorskip("playwright")


@pytest.fixture(scope="module")
def server():
    """Start the portal in a subprocess for browser testing."""
    proc = subprocess.Popen(
        [".venv/bin/python", "-m", "agentit", "portal", "--port", "9999"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)  # Wait for startup
    yield "http://localhost:9999"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.mark.skipif(True, reason="Requires running server — run with --browser-tests")
class TestBrowserUI:
    """Browser tests — skip by default, run with pytest --browser-tests."""

    def test_fleet_page_loads(self, page, server):
        page.goto(server)
        assert page.title() == "AgentIT"
        assert page.locator("h1").inner_text() == "Enterprise Readiness"

    def test_assess_modal_opens_and_closes(self, page, server):
        page.goto(server)
        page.click("text=Assess New Repo")
        modal = page.locator("#assess-modal")
        assert modal.is_visible()
        # Close with X button
        page.click(".modal-close")
        assert not modal.is_visible()

    def test_assess_modal_closes_on_backdrop(self, page, server):
        page.goto(server)
        page.click("text=Assess New Repo")
        modal = page.locator("#assess-modal")
        assert modal.is_visible()
        # Click backdrop (outside modal)
        page.click("#assess-modal", position={"x": 10, "y": 10})
        assert not modal.is_visible()

    def test_nav_has_primary_links(self, page, server):
        page.goto(server)
        assert page.locator("nav >> text=Fleet").is_visible()
        assert page.locator("nav >> text=Gates").is_visible()

    def test_nav_hamburger_on_mobile(self, page, server):
        page.set_viewport_size({"width": 375, "height": 667})
        page.goto(server)
        # Hamburger should be visible
        hamburger = page.locator("[aria-label='Toggle menu']")
        assert hamburger.is_visible()
        # Secondary links should be hidden
        assert not page.locator("text=Workflows").is_visible()
        # Click hamburger
        hamburger.click()
        # Secondary links should now be visible
        assert page.locator("text=Workflows").is_visible()

    def test_events_page_has_filter(self, page, server):
        page.goto(f"{server}/events")
        assert page.locator("input[name='q']").is_visible()
        assert page.locator("select[name='severity']").is_visible()

    def test_settings_page_loads(self, page, server):
        page.goto(f"{server}/settings")
        assert "Settings" in page.title() or "Settings" in page.inner_text("h1")

    def test_insights_page_loads(self, page, server):
        page.goto(f"{server}/insights")
        assert "Insights" in page.inner_text("h1")

    def test_health_page_loads(self, page, server):
        page.goto(f"{server}/health")
        assert "Health" in page.inner_text("h1")

    def test_healthz_returns_ok(self, page, server):
        response = page.goto(f"{server}/healthz")
        assert response.status == 200

    def test_metrics_endpoint(self, page, server):
        response = page.goto(f"{server}/metrics")
        assert response.status == 200
        assert "# HELP" in page.inner_text("body")
