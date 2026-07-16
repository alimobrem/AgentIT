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

from conftest import make_report


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    """Run the portal via ASGI testclient on a local port."""
    import threading
    import uvicorn
    from agentit.models import DimensionScore, Finding, Severity
    from agentit.portal.app import app
    from agentit.portal.store import AssessmentStore
    from conftest import make_report

    store = AssessmentStore(":memory:")
    async_store = store
    # A real assessment always scores all 7 analyzer dimensions (see
    # analyzers/*.py's dimension= literals) -- make_report()'s single-dimension
    # default is fine for unit tests that don't care about score breakdown,
    # but this fixture backs the shared assessment-detail page every browser
    # test hits, so give it the full realistic set.
    dimensions = ["security", "infrastructure", "observability", "ha_dr",
                  "data_governance", "compliance", "cicd"]
    report = make_report(scores=[
        DimensionScore(
            dimension=dim, score=80, max_score=100,
            findings=[Finding(category="test", severity=Severity.low,
                              description="minor", recommendation="fix")],
        )
        for dim in dimensions
    ])
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
        # Vertical Dry Run → Apply path; short labels; status outside CTAs.
        expect(page.locator(".delivery-actions")).to_be_visible()
        expect(page.locator(".delivery-primary")).to_be_visible()
        expect(page.locator(".delivery-connector")).to_be_attached()
        expect(page.locator(".delivery-secondary")).to_be_visible()
        dry_run = page.locator(".delivery-primary button", has_text="Dry Run")
        apply_btn = page.locator(".delivery-primary button", has_text="Apply")
        expect(dry_run).to_be_visible()
        expect(apply_btn).to_be_visible()
        expect(apply_btn).not_to_contain_text("No dry run yet")
        expect(page.locator(".delivery-step-status", has_text="No dry run yet")).to_be_visible()
        expect(page.locator(".delivery-secondary button", has_text="Per-Agent PRs")).to_be_visible()
        expect(page.locator(".delivery-secondary a", has_text="Download")).to_be_visible()

    def test_remediations_page(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}/remediations")
        expect(page.locator("h1")).to_be_visible()

    def test_slos_page(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}/slos")
        expect(page.locator("h1")).to_be_visible()

    def test_gates_page_redirects_to_admin_review(self, page: Page, app_url):
        """The global Gates page is retired -- /gates now 301s to
        /admin-review (kept as a redirect, not a 404, for any stale
        bookmark/link -- see routes/gates.py's gates_page_redirect())."""
        url, _, _ = app_url
        page.goto(f"{url}/gates")
        assert page.url == f"{url}/admin-review"
        expect(page.locator("h1")).to_contain_text("Admin Review")

    def test_events_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/events")
        # docs/ui-redesign-proposal.md §5 explicitly lists Events as
        # "Unchanged" and "not recommended" for touching -- the page's h1 is,
        # and always has been, "Events" (events.html:9); only the <title>
        # tag says "Agent Activity Feed".
        expect(page.locator("h1")).to_contain_text("Events")

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
        expect(page.locator("nav >> text=Admin Review")).to_be_visible()
        expect(page.locator("nav >> text=Ledger")).to_be_visible()
        # Gates was retired as a standalone nav concept -- the 7 app-owner
        # gate types now surface via Fleet's "Needs Action" badge + each
        # app's own Actions tab; only cluster-admin-review still gets a
        # dedicated page/nav entry (Admin Review).
        assert page.locator('nav a[href="/gates"]').count() == 0
        # Events is a bell icon (not a primary-nav text link); Decisions
        # lives in the user/main menu -- neither is a top-level text item.
        assert page.locator('nav .links a[href="/events"]').count() == 0
        assert page.locator('nav .links a[href="/decisions"]').count() == 0
        assert page.locator(".activity-menu").count() == 0

    def test_secondary_nav_links(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        # "Agents" and "Workflows" haven't been standalone nav items since
        # c274055 paired them into Capabilities (Registry/Catalog tabs,
        # base.html) as part of the 9->7 top-level-items consolidation
        # docs/ui-redesign-proposal.md builds on -- check the current
        # top-level items instead (Fleet/Admin Review/Ledger are covered by
        # test_primary_nav_links above).
        for link in ["Health", "Insights"]:
            expect(page.locator(f"nav >> text={link}")).to_be_visible()
        # Events is a notification-bell icon that opens a drawer (real
        # events from /api/events); full page still at /events.
        bell = page.locator(".events-bell")
        expect(bell).to_be_visible()
        expect(bell).to_have_attribute("aria-expanded", "false")
        bell.click()
        expect(page.locator("#events-drawer-panel")).to_be_visible()
        expect(bell).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#events-drawer-panel >> text=View all")).to_be_visible()
        # Focus moves into the dialog on open (close control), then returns
        # to the bell on close -- same pattern as confirm modal / Cmd+K.
        expect(page.locator(".events-drawer-close")).to_be_focused()
        page.click(".events-drawer-close")
        expect(page.locator("#events-drawer-panel")).to_be_hidden()
        expect(bell).to_be_focused()
        expect(bell).to_have_attribute("aria-expanded", "false")
        # Capabilities/Settings/Schedules/Decisions live in the user/main
        # menu (base.html's .user-menu) -- closed by default.
        page.click(".user-menu-trigger")
        for link in ["Capabilities", "Settings", "Schedules", "Decisions"]:
            expect(page.locator(f".user-menu-dropdown >> text={link}")).to_be_visible()

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
        expect(hamburger).to_have_attribute("aria-expanded", "false")
        # Primary Fleet…Insights + Events/account are collapsed until open.
        expect(page.locator("#nav-primary")).to_be_hidden()
        expect(page.locator("#nav-secondary")).to_be_hidden()
        hamburger.click()
        expect(hamburger).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#nav-primary >> text=Fleet")).to_be_visible()
        expect(page.locator("#nav-primary >> text=Insights")).to_be_visible()
        expect(page.locator("#nav-secondary .events-bell")).to_be_visible()
        expect(page.locator("#nav-secondary .user-menu-trigger")).to_be_visible()

    def test_events_bell_badge_from_real_events(self, page: Page, app_url):
        """Unread/critical badge uses /api/events + last-seen; hide when zero."""
        url, _, _ = app_url
        # First visit with no last-seen: critical/high from the live API
        # badge the bell. Route a real-shaped payload so the assertion is
        # deterministic (browser fixture store may only have info events).
        page.route("**/api/events**", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='[{"id":"1","timestamp":"2099-01-01T00:00:00+00:00","severity":"critical","action":"alert","summary":"badge test","agent_id":"t","target_app":"a"}]',
        ))
        page.goto(url)
        page.evaluate("() => localStorage.removeItem('agentit.events.lastSeenAt')")
        page.reload()
        bell = page.locator(".events-bell")
        badge = page.locator(".events-bell-badge")
        expect(bell).to_be_visible()
        expect(badge).to_be_visible()
        expect(badge).to_have_text("1")
        expect(bell).to_have_attribute("aria-label", "Open events feed, 1 unread")
        # Opening the drawer marks last-seen and clears the badge.
        bell.click()
        expect(page.locator("#events-drawer-panel")).to_be_visible()
        page.click(".events-drawer-close")
        expect(badge).to_be_hidden()
        expect(bell).to_have_attribute("aria-label", "Open events feed")

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
        # fleet.html's stat grid is deliberately gated on total_apps > 1
        # ("Stat grid only shows for 2+ apps (noise for single app)" --
        # a75785b) -- the shared fixture only seeds one app, so add a
        # second here rather than asserting on a single-app fleet the
        # app intentionally renders without stat cards.
        url, _, store = app_url
        store.save(make_report(repo_name="browser-stat-cards-app"))
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
        # Settings also has prose referencing "the decision matrix above" --
        # a legitimate, correct second (case-insensitive) substring match for
        # the bare "text=" locator -- scope to the section heading itself to
        # avoid the Playwright strict-mode violation.
        expect(page.locator("h2.section-title", has_text="Decision Matrix")).to_be_visible()

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


# ── Admin Review Page Tests ──────────────────────────────────────────
#
# docs/ui-redesign-proposal.md §2/§5: the retired global Gates page's
# replacement. Intentionally narrow -- only `cluster-admin-review` gates,
# for whoever holds the elevated RBAC that gate type needs; every other
# gate type now lives on Fleet's "Needs Action" badge + each app's own
# Assessment Detail Actions tab (see TestAssessmentDetailActionsTab below).


class TestAdminReviewPage:
    def test_admin_review_shows_cluster_admin_review_gate(self, page: Page, app_url):
        url, _, store = app_url
        aid = store.save(make_report(repo_name="browser-admin-review-app"))
        store.create_gate(aid, "cluster-admin-review",
                           "Browser test: CI/CD manifests need elevated review")

        page.goto(f"{url}/admin-review")
        expect(page.locator("h1")).to_contain_text("Admin Review")
        # cluster-admin-review's delivery_confirmation echoes its own
        # summary verbatim (delivery.py's gate_delivery_confirmation()), so
        # the text legitimately appears twice within the one gate card --
        # scope to the card itself rather than the bare text to avoid a
        # Playwright strict-mode violation on the expected duplicate.
        gate_card = page.locator(".card", has_text="Browser test: CI/CD manifests need elevated review")
        expect(gate_card).to_be_visible()

    def test_admin_review_excludes_app_owner_gate_types(self, page: Page, app_url):
        """A gate type other than cluster-admin-review must never show up
        on this page -- it belongs on that app's own Actions tab instead."""
        url, _, store = app_url
        aid = store.save(make_report(repo_name="browser-app-owner-gate-app"))
        store.create_gate(aid, "auto-mode-review",
                           "Browser test: app-owner gate, must not appear on Admin Review")

        page.goto(f"{url}/admin-review")
        expect(page.locator("h1")).to_contain_text("Admin Review")
        assert page.locator(
            "text=Browser test: app-owner gate, must not appear on Admin Review"
        ).count() == 0


# ── Fleet "Needs Action" / GitOps Badge Tests ────────────────────────


class TestFleetBadges:
    def test_needs_action_badge_for_app_with_pending_gate(self, page: Page, app_url):
        """The shared fixture app already carries one pending, app-owner
        gate (`deploy-approval`, created in the app_url fixture above) --
        its Fleet row must show a "N pending" badge for it."""
        url, aid, _ = app_url
        page.goto(url)
        row = page.locator("tr", has_text="test-app")
        expect(row.locator("text=pending")).to_be_visible()

    def test_no_needs_action_badge_when_only_admin_review_gate_pending(self, page: Page, app_url):
        """cluster-admin-review gates must not count toward this badge --
        they're a different audience's queue (Admin Review page)."""
        url, _, store = app_url
        store.save(make_report(repo_name="browser-admin-only-fleet-app"))
        aid2 = store.save(make_report(repo_name="browser-admin-only-fleet-app-2"))
        store.create_gate(aid2, "cluster-admin-review", "Browser test: elevated review only")

        page.goto(url)
        row = page.locator("tr", has_text="browser-admin-only-fleet-app-2")
        assert row.locator("text=pending").count() == 0

    def test_gitops_badge_for_registered_app(self, page: Page, app_url):
        import time
        from agentit.portal.delivery import gitops_application_name
        from agentit.portal.routes import fleet as fleet_routes

        url, _, store = app_url
        store.save(make_report(repo_name="browser-gitops-fleet-app"))
        fake_argo = {
            "data": {gitops_application_name("browser-gitops-fleet-app"): {
                "sync": "Synced", "health": "Healthy",
                "cluster": "https://cluster", "namespace": "browser-gitops-fleet-app",
            }},
            "ts": time.monotonic(),
        }

        with patch.object(fleet_routes, "_argo_cache", fake_argo):
            page.goto(url)

        row = page.locator("tr", has_text="browser-gitops-fleet-app")
        expect(row.locator(".badge", has_text="GitOps")).to_be_visible()

    def test_direct_apply_badge_for_unregistered_app(self, page: Page, app_url):
        url, _, store = app_url
        store.save(make_report(repo_name="browser-direct-apply-fleet-app"))

        page.goto(url)
        row = page.locator("tr", has_text="browser-direct-apply-fleet-app")
        expect(row.locator(".badge", has_text="Direct apply")).to_be_visible()


# ── Assessment Detail Actions Tab Tests ──────────────────────────────
#
# The Actions tab reuses the exact same gate_card() macro/UI as the Admin
# Review page (docs/ui-redesign-proposal.md §2's "reuse the same
# partial, don't reinvent it") -- for every gate type except
# cluster-admin-review, which stays on the separate Admin Review page.


class TestAssessmentDetailActionsTab:
    def test_actions_tab_renders_gate_approval_ui(self, page: Page, app_url):
        url, _, store = app_url
        # Deliberately avoids the substring "actions" in the repo name --
        # that would make Playwright's unquoted `text=Actions` selector
        # below also match this app's own <h1>/repo-url link.
        aid = store.save(make_report(repo_name="browser-gate-approval-app"))
        store.create_gate(aid, "auto-mode-review", "Browser test: auto-mode gated pending review")

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Actions")
        # The gate's summary also legitimately appears in the (hidden,
        # x-show) Timeline tab's pseudo-event list (get_assessment_timeline()
        # surfaces gates too) -- scope to the gate_card's own `.card`
        # wrapper, the one element unique to the Actions tab's rendering,
        # so this assertion can't accidentally pass against Timeline's copy.
        gate_card = page.locator(".card", has_text="Browser test: auto-mode gated pending review")
        expect(gate_card).to_be_visible()
        expect(gate_card.locator("button:has-text('Approve')")).to_be_visible()
        # .first: "Reject" is itself a substring of the reveal-on-click
        # "Confirm Reject" button also inside this card -- only the first
        # (always-visible) one is being asserted on here.
        expect(gate_card.locator("button:has-text('Reject')").first).to_be_visible()
        expect(gate_card.locator("button:has-text('Dismiss')")).to_be_visible()

    def test_actions_tab_excludes_cluster_admin_review_gate(self, page: Page, app_url):
        url, _, store = app_url
        aid = store.save(make_report(repo_name="browser-gate-exclude-app"))
        store.create_gate(aid, "cluster-admin-review",
                           "Browser test: elevated review, must not show on Actions tab")

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Actions")
        # Same caveat as above: the gate's summary can legitimately appear
        # in the Timeline tab's pseudo-event list -- what must be absent is
        # a gate_card() *card* for it (i.e. it must never be resolvable
        # from the Actions tab UI).
        assert page.locator(
            ".card", has_text="Browser test: elevated review, must not show on Actions tab"
        ).count() == 0
        expect(page.locator("text=No pending actions for this app")).to_be_visible()


# ── Retired Routes Tests ─────────────────────────────────────────────
#
# /apply and /create-pr were fully removed (not just hidden), so any
# navigation to them -- even a plain browser GET -- must 404, same as
# test_404_page above.


class TestRetiredRoutes:
    def test_apply_route_gone(self, page: Page, app_url):
        url, aid, _ = app_url
        resp = page.goto(f"{url}/assessments/{aid}/apply")
        assert resp.status == 404

    def test_create_pr_route_gone(self, page: Page, app_url):
        url, aid, _ = app_url
        resp = page.goto(f"{url}/assessments/{aid}/create-pr")
        assert resp.status == 404


# ── Self-Improvement "Run Now" Button Test ───────────────────────────


class TestSelfImprovementRunButton:
    def test_run_button_present_and_clickable(self, page: Page, app_url):
        """Clicking the button must not trigger a real capability-scout
        cycle (LLM calls, git clone, etc.) in this test -- stub
        CapabilityScout.research_once() the same way test_ui_redesign.py's
        equivalent TestClient-level test does."""
        url, _, _ = app_url
        with patch("agentit.watchers.capability_scout.CapabilityScout.research_once",
                   return_value={"outcome": "no-signal"}):
            page.goto(f"{url}/capabilities/self-improvement")
            button = page.locator("button:has-text('Run Self-Improvement Scan')")
            expect(button).to_be_visible()
            expect(button).to_be_enabled()
            button.click()
            # The redirect target's own URL (not just "still on this page")
            # is what proves the round trip actually completed -- the
            # pre-click URL already matches a bare "self-improvement"
            # substring, so waiting on that alone would return immediately
            # without the click's request/redirect ever completing.
            page.wait_for_url(re.compile(r".*warning=.*"))
        assert "warning=" in page.url


# ── Fix Button Visibility Tests ──────────────────────────────────────
#
# Whether a finding shows a Fix button is driven entirely by
# remediation/registry.py's FIX_REGISTRY (via fixable_categories in
# routes/assessments.py) -- a category with no registered skill must
# render no Fix button at all, in the real page, not just per a
# TestClient string-search assertion.


class TestFixButtonVisibility:
    def test_fix_button_shown_only_for_registered_category(self, page: Page, app_url):
        from agentit.models import DimensionScore, Finding, Severity

        url, _, store = app_url
        report = make_report(
            repo_name="browser-fix-button-app",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="Browser test: no NetworkPolicy", recommendation="Add one"),
                Finding(category="totally_unregistered_category", severity=Severity.high,
                        description="Browser test: unmatched finding", recommendation="n/a"),
            ])],
        )
        aid = store.save(report)

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Findings")

        matched_item = page.locator(".finding-item", has_text="Browser test: no NetworkPolicy")
        expect(matched_item.locator("button:has-text('Fix')")).to_be_visible()

        unmatched_item = page.locator(".finding-item", has_text="Browser test: unmatched finding")
        assert unmatched_item.locator("button:has-text('Fix')").count() == 0


# ── UX Requirements: Confirm Modal Focus + Type-to-Confirm (#1, #2) ──────
#
# docs/ux-design-requirements.md checklist #2: Cancel must receive default
# focus on every confirm, destructive or not (a reflexive Enter must never
# fire the guarded action). #1: type-to-confirm is reserved for the two
# highest-blast-radius actions in the app (Delete App, cluster-admin-review
# gate approval) -- both genuinely interaction-level, so covered here rather
# than only asserting markup presence in the TestClient-level tests.


class TestConfirmModalFocusAndTypeToConfirm:
    def test_cancel_receives_default_focus_on_open(self, page: Page, app_url):
        """A routine (non-destructive) confirm -- Register for GitOps --
        must ALSO default-focus Cancel, not just destructive ones."""
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        page.click("button:has-text('Delete')")
        expect(page.locator("#confirm-modal")).to_have_class(re.compile("open"))
        expect(page.locator("#confirm-modal button:has-text('Cancel')")).to_be_focused()

    def test_reflexive_enter_does_not_fire_destructive_action(self, page: Page, app_url):
        """With Cancel focused, pressing Enter must dismiss/no-op, never
        submit the guarded destructive form."""
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        page.click("button:has-text('Delete')")
        cancel_btn = page.locator("#confirm-modal button:has-text('Cancel')")
        expect(page.locator("#confirm-modal")).to_have_class(re.compile("open"))
        expect(cancel_btn).to_be_focused()
        # Locator.press() (unlike the global page.keyboard.press()) targets
        # this specific element directly rather than depending on which
        # page/tab the OS-level input focus happens to be on -- the more
        # deterministic way to assert "pressing Enter while Cancel is
        # focused" in an automated, possibly-multi-page browser session.
        cancel_btn.press("Enter")
        # The modal closed (Cancel's own handler ran) and we're still on
        # the assessment page -- a fired delete would have redirected to "/".
        expect(page.locator("#confirm-modal")).not_to_have_class(re.compile("open"))
        assert f"/assessments/{aid}" in page.url

    def test_delete_app_requires_typing_exact_name(self, page: Page, app_url):
        url, _, store = app_url
        aid = store.save(make_report(repo_name="type-confirm-app"))
        page.goto(url)
        row = page.locator("tr", has_text="type-confirm-app")
        row.locator("button[aria-label='Delete type-confirm-app']").click()

        expect(page.locator("#confirm-modal")).to_have_class(re.compile("open"))
        confirm_btn = page.locator("#confirm-modal button", has_text="I understand, delete this app")
        expect(confirm_btn).to_be_visible()
        expect(confirm_btn).to_be_disabled()

        type_input = page.locator("#type-confirm-input")
        expect(type_input).to_be_visible()
        type_input.fill("wrong-name")
        expect(confirm_btn).to_be_disabled()

        type_input.fill("type-confirm-app")
        expect(confirm_btn).to_be_enabled()

    def test_delete_app_confirm_actually_deletes_once_enabled(self, page: Page, app_url):
        url, _, store = app_url
        aid = store.save(make_report(repo_name="type-confirm-delete-app"))
        page.goto(url)
        row = page.locator("tr", has_text="type-confirm-delete-app")
        row.locator("button[aria-label='Delete type-confirm-delete-app']").click()

        page.locator("#type-confirm-input").fill("type-confirm-delete-app")
        page.locator("#confirm-modal button", has_text="I understand, delete this app").click()

        expect(page.locator("#confirm-modal")).not_to_have_class(re.compile("open"))
        page.wait_for_url(re.compile(r"^" + re.escape(url) + r"/?$"))
        assert store.get(aid) is None

    def test_ordinary_confirm_has_no_type_to_confirm_input(self, page: Page, app_url):
        """A routine confirm (Reject via the Actions tab's Dismiss button)
        must never show the type-to-confirm input -- overusing it cheapens
        the pattern (checklist #1's own warning)."""
        url, _, store = app_url
        aid = store.save(make_report(repo_name="browser-ordinary-gate-app"))
        store.create_gate(aid, "auto-mode-review", "Browser test: ordinary gate")
        page.goto(f"{url}/assessments/{aid}?tab=actions")
        gate_card = page.locator(".card", has_text="Browser test: ordinary gate")
        gate_card.locator("button:has-text('Dismiss')").click()
        expect(page.locator("#confirm-modal")).to_have_class(re.compile("open"))
        expect(page.locator("#type-confirm-input")).not_to_be_visible()
        # Regression guard: Alpine's boolean-attribute normalization only
        # clears :disabled for an expression that evaluates to exactly
        # null/undefined/false. `typedConfirmTarget && ...` short-circuits to
        # the falsy string '' (not boolean false) for every ordinary confirm,
        # which Alpine previously (mis)treated as truthy and left the button
        # permanently disabled -- see the onboard-results "Deliver Now" bug.
        expect(page.locator("#confirm-modal button", has_text="Dismiss")).to_be_enabled()


# ── UX Requirements: Command Palette (#4, #5) ────────────────────────────


class TestCommandPalette:
    def test_shortcut_hint_visible_in_nav(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        expect(page.locator(".cmdk-trigger")).to_be_visible()
        expect(page.locator(".cmdk-trigger")).to_contain_text("K")

    def test_ctrl_k_opens_palette_from_any_page(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/settings")
        expect(page.locator("#command-palette")).not_to_have_class(re.compile("open"))
        page.keyboard.press("Control+k")
        expect(page.locator("#command-palette")).to_have_class(re.compile("open"))
        expect(page.locator("#command-palette input")).to_be_focused()

    def test_click_trigger_opens_palette(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        page.click(".cmdk-trigger")
        expect(page.locator("#command-palette")).to_have_class(re.compile("open"))

    def test_escape_closes_palette(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        page.keyboard.press("Control+k")
        expect(page.locator("#command-palette")).to_have_class(re.compile("open"))
        page.keyboard.press("Escape")
        expect(page.locator("#command-palette")).not_to_have_class(re.compile("open"))

    def test_search_filters_nav_items(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        page.keyboard.press("Control+k")
        page.locator("#command-palette input").fill("admin")
        results = page.locator("#command-palette .cmdk-item")
        expect(results.first).to_contain_text("Admin Review")

    def test_search_finds_real_fleet_app_and_navigates(self, page: Page, app_url):
        url, _, store = app_url
        store.save(make_report(repo_name="palette-findable-app"))
        page.goto(url)
        page.keyboard.press("Control+k")
        page.locator("#command-palette input").fill("palette-findable-app")
        result = page.locator("#command-palette .cmdk-item", has_text="palette-findable-app")
        expect(result).to_be_visible()
        result.click()
        page.wait_for_url(re.compile(r".*/assessments/.*"))
        expect(page.locator("h1")).to_contain_text("palette-findable-app")

    def test_enter_key_opens_top_result(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/settings")
        page.keyboard.press("Control+k")
        page.locator("#command-palette input").fill("events")
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r".*/events.*"))


# ── UX Requirements: Optimistic Suppress (#7) ────────────────────────────


class TestOptimisticSuppress:
    def test_suppress_hides_finding_immediately(self, page: Page, app_url):
        from agentit.models import DimensionScore, Finding, Severity

        url, _, store = app_url
        report = make_report(
            repo_name="browser-optimistic-suppress-app",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="secrets", severity=Severity.high,
                        description="Browser test: suppress me", recommendation="n/a",
                        source="trivy"),
            ])],
        )
        aid = store.save(report)

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Findings")
        item = page.locator(".finding-item", has_text="Browser test: suppress me")
        expect(item).to_be_visible()

        item.locator("button:has-text('Suppress')").click()
        item.locator("input[name='reason']").fill("handled externally")
        item.locator("button:has-text('Confirm')").click()

        # Optimistic: hidden immediately, without a full-page navigation
        # (the URL never changes -- a full boosted-redirect reload would
        # change it back to /assessments/{aid} with no ?tab, losing the
        # Findings-tab context).
        expect(item).not_to_be_visible(timeout=2000)
        assert page.url == f"{url}/assessments/{aid}"

        # The real, underlying suppression genuinely persisted server-side
        # (not just a client-side illusion) -- suppress_check() is a
        # forward-looking record (it stops the check firing on FUTURE
        # assessments; it doesn't rewrite this already-completed report's
        # stored findings), so this is the correct way to verify the real
        # outcome, not by expecting THIS historical finding to vanish on
        # reload.
        assert store.get_suppressions("browser-optimistic-suppress-app")

    def test_suppress_failure_reconciles_by_restoring_the_finding(self, page: Page, app_url):
        """If the real request fails, the optimistically-hidden finding
        must come back -- the prediction reconciles with reality, it never
        just silently stays wrong (Vercel's optimistic-UI principle, bounded
        by this app's own fail-closed posture)."""
        from agentit.models import DimensionScore, Finding, Severity

        url, _, store = app_url
        report = make_report(
            repo_name="browser-optimistic-suppress-fail-app",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="secrets", severity=Severity.high,
                        description="Browser test: suppress failure", recommendation="n/a",
                        source="trivy"),
            ])],
        )
        aid = store.save(report)

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Findings")
        item = page.locator(".finding-item", has_text="Browser test: suppress failure")

        with page.route("**/api/suppress", lambda route: route.fulfill(status=500, body="error")):
            item.locator("button:has-text('Suppress')").click()
            item.locator("input[name='reason']").fill("handled externally")
            item.locator("button:has-text('Confirm')").click()
            # Reconciliation: back to visible after the failed request.
            expect(item).to_be_visible(timeout=2000)
        # A visible error toast, per CLAUDE.md's "errors must always be
        # visible to the user" -- reusing base.html's existing global
        # htmx:responseError toast handler, not a bespoke one.
        expect(page.locator(".toast-error")).to_be_visible()
