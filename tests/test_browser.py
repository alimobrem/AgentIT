"""Comprehensive browser tests using Playwright — every page, every action.

Run with: pytest tests/test_browser.py -v
Requires: pip install pytest-playwright && playwright install chromium
These use the FastAPI TestClient with ASGI transport (no subprocess needed).
"""
from __future__ import annotations

import asyncio
import re
import socket
import threading
import time as _time

import pytest
from unittest.mock import patch

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright

from conftest import _ALL_STORE_TABLES, _resolve_postgres_dsn, make_report


@pytest.fixture(scope="module")
def _browser():
    """One Chromium instance for the whole module -- launching a fresh
    browser per test (83 of them in this file) is needlessly slow; a
    fresh `context` (below) per test still gives each test a clean
    cookie/storage slate, same as ``pytest-playwright``'s own default
    session-scoped-browser/function-scoped-context split."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(_browser):
    """A plain Playwright ``Page``, managed directly via ``sync_playwright()``.

    This project depends on bare ``playwright``, not the ``pytest-playwright``
    plugin (which would otherwise supply this fixture) -- confirmed by
    ``pyproject.toml``'s ``browser`` extra and the lockfile, and matching
    ``test_browser_critical.py``'s own precedent of managing Playwright
    directly rather than relying on plugin-provided fixtures (there via
    ``async_playwright()``; here via the sync counterpart, since every test
    body in this file already uses the synchronous Playwright API).
    """
    context = _browser.new_context()
    pg = context.new_page()
    try:
        yield pg
    finally:
        context.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _SyncStoreBridge:
    """Wraps a real async ``AssessmentStore`` so its coroutine methods can
    be called synchronously -- every ``store.save(...)``/``store.log_event(...)``
    call already scattered across this file's (deliberately synchronous,
    ``playwright.sync_api``-based) test bodies keeps working unchanged.

    Marshals each call onto ``loop`` (the single dedicated background event
    loop this fixture also runs the real uvicorn server on) via
    ``asyncio.run_coroutine_threadsafe`` -- the same bridge
    ``fleet.py::_enrich_fleet_with_cluster_status``'s own ``_bridge()``
    helper uses for the identical constraint: an ``asyncpg`` pool is bound
    to the loop that created it and can't be driven from a different one.
    """

    def __init__(self, store, loop: asyncio.AbstractEventLoop):
        self._store = store
        self._loop = loop

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if not asyncio.iscoroutinefunction(attr):
            return attr

        def _call(*args, **kwargs):
            future = asyncio.run_coroutine_threadsafe(attr(*args, **kwargs), self._loop)
            return future.result(timeout=30)

        return _call


@pytest.fixture(scope="module")
def app_url():
    """Run the real portal app (real async ``AssessmentStore`` against a
    real Postgres, real uvicorn server) on a dedicated background event
    loop, exposing a synchronous store bridge so every existing
    ``playwright.sync_api`` test body in this file -- including the ones
    that call ``store.save(...)``/``store.log_event(...)`` etc directly --
    keeps working unchanged.

    Previously constructed ``AssessmentStore(":memory:")`` (no such
    constructor exists anymore; ``AssessmentStore.__init__`` takes an
    ``asyncpg.Pool``) and called its ``async def`` methods without
    ``await``, so every one of them was a silent no-op returning an
    unawaited coroutine -- ``aid`` was always ``None`` and the pages under
    test never actually had any seeded data.
    """
    import uvicorn
    from agentit.models import DimensionScore, Finding, Severity
    from agentit.portal.app import app
    from agentit.portal.store import AssessmentStore

    dsn = _resolve_postgres_dsn()
    if dsn is None:
        pytest.skip("no AGENTIT_TEST_PG_DSN and no podman/docker on PATH to start one")

    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop, daemon=True)
    loop_thread.start()

    def _run_on_loop(coro, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    async def _make_store() -> AssessmentStore:
        s = await AssessmentStore.create(dsn, min_size=1, max_size=4)
        async with s._pool.acquire() as conn:
            await conn.execute(f"TRUNCATE {', '.join(_ALL_STORE_TABLES)} CASCADE")
        return s

    async_store = _run_on_loop(_make_store())
    store = _SyncStoreBridge(async_store, loop)

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

    async def _noop_close(_self=None) -> None:
        return None

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.helpers.get_store", return_value=async_store), \
         patch("agentit.portal.helpers._store", async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=async_store), \
         patch.object(AssessmentStore, "close", _noop_close):

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        # pytest (this thread) owns signals -- uvicorn must not install its
        # own handlers on a loop that isn't running on the main thread.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        serve_future = asyncio.run_coroutine_threadsafe(server.serve(), loop)

        import httpx
        deadline = _time.monotonic() + 15
        while _time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            _time.sleep(0.1)
        else:
            raise RuntimeError(f"portal did not become ready at {url}")

        yield url, aid, store

        server.should_exit = True
        try:
            serve_future.result(timeout=5)
        except Exception:
            pass

    _run_on_loop(async_store._pool.close())
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)


# ── Page Load Tests ──────────────────────────────────────────────────


class TestEveryPageLoads:
    """Every HTML page returns 200 and has the expected h1."""

    def test_fleet_dashboard(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/fleet")
        expect(page.locator("h1")).to_contain_text("Fleet")
        expect(page.locator("text=Portfolio scoreboard")).to_be_visible()

    def test_root_lands_on_ledger(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        expect(page).to_have_url(f"{url}/ledger")
        expect(page.locator("h1")).to_contain_text("Ledger")

    def test_assess_redirects_to_fleet(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/assess")
        assert "assess=1" in page.url or "/fleet" in page.url

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
        # Scan opens PRs; page keeps Download + PR cards, not Commit/Per-Agent.
        expect(page.locator(".delivery-actions")).to_be_visible()
        expect(page.locator(".delivery-secondary")).to_be_visible()
        expect(page.locator("button[data-action='apply']")).to_have_count(0)
        expect(page.locator("button[data-action='prs']")).to_have_count(0)
        expect(page.locator("button[data-action='dry-run']")).to_have_count(0)
        expect(page.get_by_text("Commit & Open PR")).to_have_count(0)
        expect(page.get_by_text("Per-Agent PRs")).to_have_count(0)
        expect(page.get_by_text("Run Automatic Validation")).to_have_count(0)
        expect(page.locator("button[data-action='apply-override']")).to_have_count(0)
        expect(page.locator(".delivery-secondary a", has_text="Download")).to_be_visible()

    def test_slos_page(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}/slos")
        expect(page.locator("h1")).to_be_visible()

    def test_gates_page_redirects_to_ledger(self, page: Page, app_url):
        """The global Gates page is retired -- /gates now 301s to /ledger
        (previously /admin-review, itself retired 2026-07-18 along with the
        `cluster-admin-review` gate type it existed solely for -- kept as a
        redirect, not a 404, for any stale bookmark/link -- see routes/
        gates.py's gates_page_redirect())."""
        url, _, _ = app_url
        page.goto(f"{url}/gates")
        assert page.url == f"{url}/ledger"
        expect(page.locator("h1")).to_contain_text("Ledger")

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
        # The assess modal lives on Fleet (fleet.html) -- root now lands on
        # Ledger (docs/ui-redesign-proposal.md), which has no such modal.
        page.goto(f"{url}/fleet")
        page.click("text=Add App")
        expect(page.locator("#assess-modal")).to_have_class(re.compile("open"))

    def test_assess_modal_closes_with_x(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/fleet")
        page.click("text=Add App")
        expect(page.locator("#assess-modal")).to_have_class(re.compile("open"))
        page.click("#assess-modal .modal-close")
        expect(page.locator("#assess-modal")).not_to_have_class(re.compile("open"))

    def test_assess_modal_has_form_fields(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/fleet")
        page.click("text=Add App")
        expect(page.locator("#assess-modal input[name='repo_url']")).to_be_visible()
        expect(page.locator("#assess-modal select[name='criticality']")).to_be_visible()
        expect(page.locator("#assess-modal button[type='submit']")).to_be_visible()

    def test_edl_assess_modal_dialog_role_and_escape(self, page: Page, app_url):
        """EDL §5: assess overlay is a dialog and Escape dismisses it."""
        url, _, _ = app_url
        page.goto(f"{url}/fleet")
        page.click("text=Add App")
        modal = page.locator("#assess-modal")
        expect(modal).to_have_class(re.compile("open"))
        expect(modal).to_have_attribute("role", "dialog")
        expect(modal).to_have_attribute("aria-modal", "true")
        page.keyboard.press("Escape")
        expect(modal).not_to_have_class(re.compile("open"))

    def test_edl_confirm_modal_dialog_semantics(self, page: Page, app_url):
        """EDL §5: shared confirm modal exposes dialog role + labelled title."""
        url, _, _ = app_url
        page.goto(url)
        confirm = page.locator("#confirm-modal")
        expect(confirm).to_have_attribute("role", "dialog")
        expect(confirm).to_have_attribute("aria-modal", "true")
        expect(confirm).to_have_attribute("aria-labelledby", "confirm-modal-title")


# ── Navigation Tests ─────────────────────────────────────────────────


class TestNavigation:
    """Test navigation works correctly."""

    def test_primary_nav_links(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        expect(page.locator("nav >> text=Fleet")).to_be_visible()
        # Plain `text=Ledger` is ambiguous -- the user menu's hidden-but-
        # still-DOM-present Decisions item sub-text also mentions "Ledger"
        # in passing. Scope to the actual nav link.
        expect(page.locator('nav a[href="/ledger"]')).to_be_visible()
        # Admin Review (a third, elevated-approvals nav item) was retired
        # 2026-07-18 along with the `cluster-admin-review` gate type it
        # existed solely for -- every gate type is per-app now, so it's
        # gone from both the primary nav and the user menu dropdown.
        page.click("button[aria-label='Open account and settings menu']")
        assert page.locator(".user-menu-dropdown >> text=Admin Review").count() == 0
        # Gates was retired as a standalone nav concept -- every gate type
        # now surfaces via Fleet's "Needs Action" badge + each app's own
        # Ledger tab.
        assert page.locator('nav a[href="/gates"]').count() == 0
        assert page.locator('a[href="/admin-review"]').count() == 0
        # Events is a bell icon (not a primary-nav text link); Decisions
        # lives in the user/main menu -- neither is a top-level text item.
        assert page.locator('nav .links a[href="/events"]').count() == 0
        assert page.locator('nav .links a[href="/decisions"]').count() == 0
        assert page.locator(".activity-menu").count() == 0

    def test_secondary_nav_links(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(url)
        # Primary spine is Fleet + Ledger only. Operate surfaces (Health,
        # Insights, Events page, Decisions, DLQ, Schedules) live under Menu.
        assert page.locator('#nav-primary a[href="/health"]').count() == 0
        assert page.locator('#nav-primary a[href="/insights"]').count() == 0
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
        page.click(".user-menu-trigger")
        # Prefer role=menuitem + exact text: /health also matches deploy-status link.
        for label in (
            "Health",
            "Insights",
            "Capabilities",
            "Settings",
            "Schedules",
            "Decisions",
            "Events",
            "DLQ",
        ):
            expect(
                page.locator("#user-menu-dropdown").get_by_role("menuitem", name=label, exact=True)
            ).to_be_visible()

    def test_fleet_link_navigates(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/settings")
        page.click("nav >> text=Fleet")
        page.wait_for_url("**/fleet")

    def test_hamburger_on_mobile(self, page: Page, app_url):
        url, _, _ = app_url
        page.set_viewport_size({"width": 375, "height": 667})
        page.goto(url)
        hamburger = page.locator("[aria-label='Toggle menu']")
        expect(hamburger).to_be_visible()
        expect(hamburger).to_have_attribute("aria-expanded", "false")
        # Primary Fleet+Ledger + Events/account are collapsed until open.
        expect(page.locator("#nav-primary")).to_be_hidden()
        expect(page.locator("#nav-secondary")).to_be_hidden()
        hamburger.click()
        expect(hamburger).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#nav-primary >> text=Ledger")).to_be_visible()
        expect(page.locator("#nav-primary >> text=Fleet")).to_be_visible()
        expect(page.locator("#nav-primary >> text=Insights")).to_have_count(0)
        expect(page.locator("#nav-secondary .events-bell")).to_be_visible()
        expect(page.locator("#nav-secondary .user-menu-trigger")).to_be_visible()
        hamburger.click()
        expect(hamburger).to_have_attribute("aria-expanded", "false")
        expect(page.locator("#nav-primary")).to_be_hidden()

    def test_events_drawer_escape_and_focus_trap(self, page: Page, app_url):
        """Esc closes the drawer; Tab wraps inside the dialog (EDL a11y)."""
        url, _, _ = app_url
        page.goto(url)
        bell = page.locator(".events-bell")
        bell.click()
        panel = page.locator("#events-drawer-panel")
        expect(panel).to_be_visible()
        expect(page.locator(".events-drawer-close")).to_be_focused()
        # Tab from last focusable wraps to close; Shift+Tab from close → footer.
        page.locator("#events-drawer-panel >> text=View all").focus()
        page.keyboard.press("Tab")
        expect(page.locator(".events-drawer-close")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator("#events-drawer-panel >> text=View all")).to_be_focused()
        page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        expect(bell).to_be_focused()

    def test_events_drawer_severity_badge_class(self, page: Page, app_url):
        """Drawer rows always get a colored severity badge (incl. warning→medium)."""
        url, _, _ = app_url
        page.route("**/api/events**", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '[{"id":"1","timestamp":"2099-01-01T00:00:00+00:00","severity":"warning",'
                '"action":"drift","summary":"warn","agent_id":"t","target_app":"a"},'
                '{"id":"2","timestamp":"2099-01-02T00:00:00+00:00","severity":"critical",'
                '"action":"alert","summary":"crit","agent_id":"t","target_app":"a"},'
                '{"id":"3","timestamp":"2099-01-03T00:00:00+00:00","severity":"mystery",'
                '"action":"x","summary":"unknown sev","agent_id":"t","target_app":"a"}]'
            ),
        ))
        page.goto(url)
        page.locator(".events-bell").click()
        expect(page.locator("#events-drawer-panel")).to_be_visible()
        badges = page.locator(".events-drawer-item .badge")
        expect(badges.nth(0)).to_have_class(re.compile(r"badge-medium"))
        expect(badges.nth(0)).to_have_text("warning")
        expect(badges.nth(1)).to_have_class(re.compile(r"badge-critical"))
        expect(badges.nth(2)).to_have_class(re.compile(r"badge-info"))
        page.unroute("**/api/events**")

    def test_events_bell_badge_from_real_events(self, page: Page, app_url):
        """Severity-gated unread badge: critical/high only; info never re-badges."""
        url, _, _ = app_url
        # First visit with no last-seen: critical/high from the live API
        # badge the bell. Route a real-shaped payload so the assertion is
        # deterministic (browser fixture store may only have info events).
        page.route("**/api/events**", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='[{"id":"1","timestamp":"2099-01-01T00:00:00+00:00","severity":"critical","action":"alert","summary":"badge test","agent_id":"t","target_app":"a","assessment_id":"aid-1"}]',
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
        item = page.locator(".events-drawer-item").first
        expect(item).to_have_attribute("href", "/assessments/aid-1?tab=ledger")
        page.click(".events-drawer-close")
        expect(badge).to_be_hidden()
        expect(bell).to_have_attribute("aria-label", "Open events feed")
        # After last-seen: newer info/noise must NOT re-badge; newer critical does.
        page.unroute("**/api/events**")
        page.route("**/api/events**", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '[{"id":"2","timestamp":"2099-06-01T00:00:00+00:00","severity":"info",'
                '"action":"chatter","summary":"noise","agent_id":"t","target_app":"a"},'
                '{"id":"3","timestamp":"2099-06-02T00:00:00+00:00","severity":"high",'
                '"action":"alert","summary":"actionable","agent_id":"t","target_app":"a"}]'
            ),
        ))
        page.reload()
        expect(badge).to_be_visible()
        expect(badge).to_have_text("1")
        page.unroute("**/api/events**")
        page.route("**/api/events**", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '[{"id":"4","timestamp":"2099-07-01T00:00:00+00:00","severity":"info",'
                '"action":"chatter","summary":"still noise","agent_id":"t","target_app":"a"}]'
            ),
        ))
        page.reload()
        expect(badge).to_be_hidden()

    def test_assessment_detail_back_link(self, page: Page, app_url):
        url, aid, _ = app_url
        page.goto(f"{url}/assessments/{aid}")
        # `a[href="/fleet"]` alone is ambiguous now (nav's own Fleet link
        # plus other in-page "Fleet" links/CTAs) -- scope to the specific
        # "&larr; Fleet" back-link at the top of the page.
        expect(page.locator('p.mb-1 a[href="/fleet"]')).to_contain_text("Fleet")


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
        # The stat grid lives on Fleet (fleet.html) -- root now lands on
        # Ledger (docs/ui-redesign-proposal.md), which has no stat grid.
        page.goto(f"{url}/fleet")
        cards = page.locator(".stat-card")
        assert cards.count() >= 2

    def test_events_filter_bar(self, page: Page, app_url):
        url, _, _ = app_url
        page.goto(f"{url}/events")
        # `input[name='q']` alone is ambiguous now -- the command palette's
        # search input (base.html) happens to share the same `name`. Scope
        # to the events filter bar's own input by id.
        expect(page.locator("#filter-events-q")).to_be_visible()
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
        # /workflows redirects into Capabilities; crisp IA uses Checks /
        # Skills / Activity client sections (How Onboarding Works removed).
        page.goto(f"{url}/workflows")
        expect(page.locator(".tab-link", has_text="Checks")).to_be_visible()
        expect(page.locator("#checks-resolutions")).to_be_visible()
        page.click(".tab-link:has-text('Skills')")
        expect(page.locator("button.collapse-toggle:has-text('Skills by Domain')")).to_be_visible()


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
# The Admin Review page (docs/ui-redesign-proposal.md §2/§5's retired global
# Gates page replacement, narrowed to `cluster-admin-review` gates only) was
# itself retired 2026-07-18 along with that gate type -- every gate type
# lives on Fleet's "Needs Action" badge + each app's own Assessment Detail
# Ledger tab now (see TestAssessmentDetailLedgerTab below), including a
# stale, already-persisted `cluster-admin-review` row.


class TestAdminReviewPage:
    def test_admin_review_page_returns_404(self, page: Page, app_url):
        url, _, _store = app_url
        response = page.goto(f"{url}/admin-review")
        assert response.status == 404


# ── Fleet "Needs Action" / GitOps Badge Tests ────────────────────────


class TestFleetBadges:
    def test_quiet_ledger_pointer_for_app_with_pending_gate(self, page: Page, app_url):
        """Fleet is scoreboard-only: pending ops surface as a quiet Ledger
        pointer, not a per-row Needs Action badge."""
        url, aid, _ = app_url
        page.goto(f"{url}/fleet")
        expect(page.locator("text=need you → Ledger")).to_be_visible()

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
            page.goto(f"{url}/fleet")

        row = page.locator("tr", has_text="browser-gitops-fleet-app")
        expect(row.locator(".badge", has_text="GitOps")).to_be_visible()

    def test_direct_apply_badge_for_unregistered_app(self, page: Page, app_url):
        url, _, store = app_url
        store.save(make_report(repo_name="browser-direct-apply-fleet-app"))

        page.goto(f"{url}/fleet")
        row = page.locator("tr", has_text="browser-direct-apply-fleet-app")
        expect(row.locator(".badge", has_text="Direct apply")).to_be_visible()


# ── Assessment Detail Ledger Tab Tests ───────────────────────────────
#
# The Ledger tab (formerly Actions/Timeline/PR History, merged 2026-07-19)
# reuses the exact same gate_card() macro/UI the (now-retired) Admin Review
# page used to (docs/ui-redesign-proposal.md §2's "reuse the same partial,
# don't reinvent it") -- for every non-PR gate type, including a stale
# `cluster-admin-review` row (see
# test_stale_cluster_admin_review_gate_shows_on_own_ledger_tab above). A
# PR-backed gate type (gitops-pr-pending) no longer gets its own gate_card
# here -- it's covered by the Ledger tab's real PR list instead.


class TestAssessmentDetailLedgerTab:
    def test_ledger_tab_renders_recommendation_ui_for_non_pr_recommendations(self, page: Page, app_url):
        """`recommendation_card()` (replacing the retired `gate_card()`,
        2026-07-19) renders the real Roll Back/Dismiss actions for a
        rollback recommendation."""
        url, _, store = app_url
        report = make_report(repo_name="browser-gate-approval-app")
        # Deliberately avoids the substring "ledger" in the repo name --
        # that would make Playwright's unquoted `text=Ledger` selector
        # below also match this app's own <h1>/repo-url link.
        aid = store.save(report)
        store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "Browser test: auto-mode gated pending review")

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Ledger")
        gate_card = page.locator(".card", has_text="Browser test: auto-mode gated pending review")
        expect(gate_card).to_be_visible()
        expect(gate_card.locator("button:has-text('Roll Back')")).to_be_visible()
        expect(gate_card.locator("button:has-text('Dismiss')")).to_be_visible()

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
            button = page.locator("button:has-text('Run Scan')")
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
        # Per-finding Fix is post-onboard only; seed onboarding so the button
        # is eligible to render (registry still gates which categories).
        store.save_onboarding(aid, [
            {"category": "security", "path": "test.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test file"},
        ])

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Findings")

        matched_item = page.locator(".finding-item", has_text="Browser test: no NetworkPolicy")
        expect(matched_item.locator("button:has-text('Fix')")).to_be_visible()

        unmatched_item = page.locator(".finding-item", has_text="Browser test: unmatched finding")
        assert unmatched_item.locator("button:has-text('Fix')").count() == 0

    def test_fix_button_hidden_pre_onboard_with_next_step_copy(self, page: Page, app_url):
        """Pre-onboard Findings must not ship a Fix control — Scan (which
        always chains into onboarding) is the generation path; Fix appears
        only after onboarding."""
        from agentit.models import DimensionScore, Finding, Severity

        url, _, store = app_url
        report = make_report(
            repo_name="browser-pre-onboard-fix-hidden",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="Pre-onboard finding: no NetworkPolicy",
                        recommendation="Add one"),
            ])],
        )
        aid = store.save(report)

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Findings")

        item = page.locator(".finding-item", has_text="Pre-onboard finding: no NetworkPolicy")
        expect(item).to_be_visible()
        assert item.locator("button:has-text('Fix')").count() == 0
        # Score-first Assessment Detail: primary CTA is Scan (not Fix) pre-onboard.
        expect(page.get_by_role("button", name="Scan")).to_be_visible()

    def test_fix_button_opens_confirm_modal(self, page: Page, app_url):
        """Post-onboard Fix must open the shared confirm modal (not a silent no-op)."""
        from agentit.models import DimensionScore, Finding, Severity

        url, _, store = app_url
        report = make_report(
            repo_name="browser-fix-confirm-app",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="Confirm test: no NetworkPolicy", recommendation="Add one"),
            ])],
        )
        aid = store.save(report)
        store.save_onboarding(aid, [
            {"category": "security", "path": "test.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test file"},
        ])

        page.goto(f"{url}/assessments/{aid}")
        page.click(".tab-nav >> text=Findings")
        page.locator(".finding-item", has_text="Confirm test: no NetworkPolicy").locator(
            "button:has-text('Fix')"
        ).click()
        expect(page.locator("#confirm-modal")).to_have_class(re.compile("open"))
        expect(page.locator("#confirm-modal button:has-text('Generate Fix')")).to_be_visible()
        expect(page.locator("#confirm-modal button:has-text('Cancel')")).to_be_focused()


# ── UX Requirements: Confirm Modal Focus + Type-to-Confirm (#1, #2) ──────
#
# docs/ux-design-requirements.md checklist #2: Cancel must receive default
# focus on every confirm, destructive or not (a reflexive Enter must never
# fire the guarded action). #1: type-to-confirm is reserved for the one
# highest-blast-radius action left in the app (Delete App -- the other case
# that used to warrant it, cluster-admin-review gate approval, was retired
# 2026-07-18), genuinely interaction-level, so covered here rather than
# only asserting markup presence in the TestClient-level tests.


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
        # The delete button lives on Fleet's table (fleet.html) -- root now
        # lands on Ledger (docs/ui-redesign-proposal.md), which has no such
        # per-app delete action.
        page.goto(f"{url}/fleet")
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
        page.goto(f"{url}/fleet")
        row = page.locator("tr", has_text="type-confirm-delete-app")
        row.locator("button[aria-label='Delete type-confirm-delete-app']").click()

        page.locator("#type-confirm-input").fill("type-confirm-delete-app")
        page.locator("#confirm-modal button", has_text="I understand, delete this app").click()

        expect(page.locator("#confirm-modal")).not_to_have_class(re.compile("open"))
        # delete_assessment() redirects to "/", which itself now redirects
        # on to "/ledger" (docs/ui-redesign-proposal.md) -- the final
        # landing page after following both hops.
        page.wait_for_url(re.compile(r"^" + re.escape(url) + r"/ledger/?$"))
        assert store.get(aid) is None

    def test_ordinary_confirm_has_no_type_to_confirm_input(self, page: Page, app_url):
        """A routine, real-danger confirm (Roll Back via the Ledger tab's
        recommendation card) must never show the type-to-confirm input --
        overusing it cheapens the pattern (checklist #1's own warning)."""
        url, _, store = app_url
        report = make_report(repo_name="browser-ordinary-gate-app")
        aid = store.save(report)
        store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "Browser test: ordinary gate")
        page.goto(f"{url}/assessments/{aid}?tab=ledger")
        gate_card = page.locator(".card", has_text="Browser test: ordinary gate")
        gate_card.locator("button:has-text('Roll Back')").click()
        expect(page.locator("#confirm-modal")).to_have_class(re.compile("open"))
        expect(page.locator("#type-confirm-input")).not_to_be_visible()
        # Regression guard: Alpine's boolean-attribute normalization only
        # clears :disabled for an expression that evaluates to exactly
        # null/undefined/false. `typedConfirmTarget && ...` short-circuits to
        # the falsy string '' (not boolean false) for every ordinary confirm,
        # which Alpine previously (mis)treated as truthy and left the button
        # permanently disabled -- see the onboard-results "Deliver Now" bug.
        expect(page.locator("#confirm-modal button", has_text="Roll Back")).to_be_enabled()


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
        page.locator("#command-palette input").fill("insight")
        results = page.locator("#command-palette .cmdk-item")
        expect(results.first).to_contain_text("Insights")

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
