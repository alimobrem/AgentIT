"""CI-gated Playwright journeys for Alpine/htmx critical paths.

Kept lean on purpose — not a full portal crawl (see ``tests/test_browser.py``,
which stays ignored in the default suite). These journeys catch the class of
bugs that unit/TestClient coverage misses: soft-gate unlock after Dry Run,
Events-drawer overlay blocking Back to Assessment after hx-boost, Register
for GitOps feedback after a boosted redirect, and onboard-progress stall
escape when SSE goes quiet (#114).

Run locally / in CI::

    uv sync --extra dev --extra browser
    uv run playwright install chromium
    uv run pytest tests/test_browser_critical.py --browser-tests -q
"""
from __future__ import annotations

import asyncio
import re
import socket
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("playwright")
from playwright.async_api import async_playwright, expect

from agentit.portal.store import AssessmentStore
from conftest import _ALL_STORE_TABLES, _resolve_postgres_dsn, make_report

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_server(url: str, timeout: float = 15.0) -> None:
    # Must stay async — a blocking readiness probe starves the uvicorn
    # serve() task on the same event loop.
    import httpx

    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                resp = await client.get(f"{url}/healthz", timeout=0.5)
                if resp.status_code == 200:
                    return
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"portal did not become ready at {url}")


async def _dedicated_store() -> AssessmentStore:
    """Own pool for the in-process uvicorn server.

    App shutdown calls ``await get_store().close()``; using the session-shared
    conftest store would poison the rest of the suite. A dedicated pool plus a
    no-op ``close`` keeps journeys isolated.
    """
    dsn = _resolve_postgres_dsn()
    if dsn is None:
        pytest.skip("no AGENTIT_TEST_PG_DSN and no podman/docker on PATH to start one")
    store = await AssessmentStore.create(dsn, min_size=1, max_size=4)
    async with store._pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(_ALL_STORE_TABLES)} CASCADE")
    return store


@pytest.fixture
async def critical_portal():
    """Postgres-backed portal on a free port; store + kube patched for journeys."""
    import uvicorn
    from agentit.models import DimensionScore, Finding, Severity
    from agentit.portal.app import app

    store = await _dedicated_store()
    dimensions = [
        "security", "infrastructure", "observability", "ha_dr",
        "data_governance", "compliance", "cicd",
    ]
    report = make_report(
        repo_name="critical-browser-app",
        scores=[
            DimensionScore(
                dimension=dim, score=80, max_score=100,
                findings=[Finding(
                    category="test", severity=Severity.low,
                    description="minor", recommendation="fix",
                )],
            )
            for dim in dimensions
        ],
    )
    aid = await store.save(report)
    await store.save_onboarding(aid, [{
        "category": "skills",
        "path": "app-network-policy.yaml",
        "content": (
            "apiVersion: networking.k8s.io/v1\n"
            "kind: NetworkPolicy\n"
            "metadata:\n  name: test\n"
        ),
        "description": "network policy",
    }])

    mock_kube = MagicMock()
    mock_kube.namespace_exists.return_value = True
    mock_kube.get_api_resources.return_value = set()
    mock_kube.apply_yaml.return_value = {"applied": True, "error": None}

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    # pytest owns signals/loop — uvicorn must not install handlers here.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _noop_close(_self=None):
        return None

    with ExitStack() as stack:
        for target in (
            "agentit.portal.app.get_store",
            "agentit.portal.helpers.get_store",
            "agentit.portal.routes.webhooks.get_store",
            "agentit.portal.routes.health.get_store",
            "agentit.portal.routes.schedules.get_store",
            "agentit.portal.routes.fleet.get_store",
            "agentit.portal.routes.assessments.get_store",
            "agentit.portal.routes.capabilities.get_store",
            "agentit.portal.routes.settings.get_store",
            "agentit.portal.routes.insights.get_store",
            "agentit.portal.routes.slos.get_store",
        ):
            stack.enter_context(patch(target, return_value=store))
        stack.enter_context(patch("agentit.portal.helpers._store", store))
        stack.enter_context(patch("agentit.portal.cluster_apply.kube", mock_kube))
        stack.enter_context(patch.object(AssessmentStore, "close", _noop_close))

        serve_task = asyncio.create_task(server.serve())
        try:
            await _wait_server(url)
            yield url, aid, store, mock_kube
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                serve_task.cancel()
            # Really close after the no-op patch exits with the ExitStack.
    await store._pool.close()


@pytest.fixture
async def page(critical_portal):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        pg = await context.new_page()
        try:
            yield pg
        finally:
            await context.close()
            await browser.close()


class TestOnboardResultsScanOnlyDelivery:
    """Onboard Results must not offer Commit & Open PR / Per-Agent /
    Run Automatic Validation as competing deliver CTAs — Scan opens PRs."""

    async def test_onboard_results_has_no_manual_deliver_ctas(self, page, critical_portal):
        url, aid, store, _kube = critical_portal
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")

        await page.goto(f"{url}/assessments/{aid}/onboard-results")
        await expect(page.locator("h1")).to_contain_text("Onboarding")
        await expect(page.locator("button[data-action='apply']")).to_have_count(0)
        await expect(page.locator("button[data-action='prs']")).to_have_count(0)
        await expect(page.locator("button[data-action='dry-run']")).to_have_count(0)
        await expect(page.get_by_text("Commit & Open PR")).to_have_count(0)
        await expect(page.get_by_text("Per-Agent PRs")).to_have_count(0)
        await expect(page.get_by_text("Run Automatic Validation")).to_have_count(0)
        await expect(page.locator(".delivery-secondary a", has_text="Download")).to_be_visible()


class TestBackToAssessmentAfterBoost:
    """#72 regression: Events drawer overlay must not swallow Back clicks
    after an hx-boost body swap."""

    async def test_back_to_assessment_clickable_after_boosted_nav(self, page, critical_portal):
        url, aid, _store, _kube = critical_portal

        await page.goto(f"{url}/fleet")
        await expect(page.locator("h1")).to_contain_text("Fleet")
        # Boosted hop into onboard-results (body hx-boost=true).
        await page.goto(f"{url}/assessments/{aid}/onboard-results")
        await page.locator('a[href="/fleet"]').first.click()
        await page.wait_for_url(re.compile(r".*/fleet"))
        await page.goto(f"{url}/assessments/{aid}/onboard-results")

        overlay = page.locator(".events-drawer-overlay")
        await expect(overlay).not_to_have_class(re.compile(r"\bopen\b"))
        back = page.locator('[data-nav="back-to-assessment"]').first
        await expect(back).to_be_visible()

        handle = await back.element_handle()
        assert handle is not None
        clickable = await page.evaluate(
            """(el) => {
              const r = el.getBoundingClientRect();
              const x = r.left + r.width / 2;
              const y = r.top + r.height / 2;
              const top = document.elementFromPoint(x, y);
              return !!top && (el === top || el.contains(top));
            }""",
            handle,
        )
        assert clickable, "Events drawer overlay (or another layer) is blocking Back to Assessment"

        await back.click()
        await page.wait_for_url(re.compile(rf".*/assessments/{re.escape(aid)}$"))
        await expect(page.locator("h1")).to_contain_text("critical-browser-app")


class TestRegisterForGitOpsFeedback:
    """Register for GitOps must surface success/error after hx-boost redirect."""

    async def test_register_gitops_shows_success_after_boost(self, page, critical_portal):
        url, aid, _store, _kube = critical_portal

        # A human-supplied repo now goes through the bring-your-own-repo
        # existence/access/create check (`github_pr.ensure_custom_gitops_repo()`,
        # see `routes/assessments.py::register_gitops()`), which calls the real
        # GitHub API and needs GITHUB_TOKEN -- neither of which this UI-feedback
        # journey should depend on. Mocked the same way `test_ui_redesign.py`'s
        # `test_register_gitops_route_sets_infra_repo_url_and_ensures_applicationset`
        # already does for the non-browser route test.
        with patch("agentit.portal.github_pr.ensure_applicationset", return_value=True), \
             patch(
                 "agentit.portal.github_pr.ensure_custom_gitops_repo",
                 return_value={
                     "repo_url": "https://github.com/org/agentit-gitops",
                     "created": False,
                 },
             ), \
             patch(
                 "agentit.portal.delivery.kube.get_custom_resource",
                 return_value=None,
             ):
            await page.goto(f"{url}/assessments/{aid}")
            await expect(
                page.locator("button[data-action='register-gitops']"),
            ).to_be_visible()

            await page.fill(
                "#register-infra-repo-url",
                "https://github.com/org/agentit-gitops",
            )
            await page.locator("button[data-action='register-gitops']").click()
            await expect(page.locator("#confirm-modal")).to_have_class(re.compile(r"open"))
            await page.locator("#confirm-modal button", has_text="Register").click()

            # Success via inline alert and/or URL-param toast after boost settle.
            success = page.locator(
                ".alert-success, .toast-success",
                has_text=re.compile(r"GitOps|infra repo", re.I),
            )
            await expect(success.first).to_be_visible(timeout=10000)
            await expect(
                page.locator("button[data-action='register-gitops']"),
            ).to_have_count(0)

    async def test_register_gitops_shows_error_after_boost(self, page, critical_portal):
        url, aid, _store, _kube = critical_portal

        with patch(
            "agentit.portal.routes.assessments._auto_create_infra_repo",
            return_value=None,
        ):
            await page.goto(f"{url}/assessments/{aid}")
            # Leave infra URL blank so auto-create runs (and fails → error flash).
            await page.locator("button[data-action='register-gitops']").click()
            await expect(page.locator("#confirm-modal")).to_have_class(re.compile(r"open"))
            await page.locator("#confirm-modal button", has_text="Register").click()

            err = page.locator(
                ".alert-error, .toast-error",
                has_text=re.compile(r"Could not auto-create|infra repo", re.I),
            )
            await expect(err.first).to_be_visible(timeout=10000)


class TestOnboardProgressStallEscape:
    """#114: a silently stalled SSE must not trap humans on the spinner.

    Uses Playwright's clock so the 20s / 60s stall windows finish instantly
    in CI; the template contract itself is also asserted in
    ``test_template_rendering.py::TestOnboardProgressStallEscape``.
    """

    async def test_hard_escape_to_onboard_results_after_long_stall(
        self, page, critical_portal,
    ):
        url, aid, store, _kube = critical_portal
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(
            job_id, "running", "Running onboarding agents...",
        )

        # Capture stall-fetch Accept header before location.replace wipes the
        # page; credentials: 'same-origin' is asserted on the template contract.
        progress_fetches: list[str] = []

        def _on_request(request) -> None:
            if (
                request.resource_type == "fetch"
                and f"/onboard/progress/{job_id}" in request.url
            ):
                progress_fetches.append(request.headers.get("accept", ""))

        page.on("request", _on_request)
        await page.clock.install()
        await page.goto(f"{url}/assessments/{aid}/onboard/progress/{job_id}")
        await expect(page.locator("[sse-connect]")).to_be_attached()

        # Past stallMs (20s) the fallback fetch runs; job still running so
        # no redirect. Past stallMs*3 the hard location.replace fires.
        await page.clock.fast_forward(65000)
        await page.wait_for_url(
            re.compile(rf".*/assessments/{re.escape(aid)}/onboard-results"),
            timeout=10000,
        )
        assert progress_fetches, "stall fallback must re-fetch the progress URL"
        assert any("text/html" in accept for accept in progress_fetches)

    async def test_follow_redirect_escape_when_job_becomes_terminal(
        self, page, critical_portal,
    ):
        url, aid, store, _kube = critical_portal
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(
            job_id, "running", "Running onboarding agents...",
        )

        await page.clock.install()
        await page.goto(f"{url}/assessments/{aid}/onboard/progress/{job_id}")
        await expect(page.locator("[sse-connect]")).to_be_attached()

        await store.update_remediation_job(
            job_id, "needs_attention",
            "Onboarding timed out during automatic validation/delivery -- review manifests.",
            error="Operation timed out after 600s",
        )
        # One stall window is enough: fetch follows the 303 to onboard-results.
        await page.clock.fast_forward(25000)
        await page.wait_for_url(
            re.compile(rf".*/assessments/{re.escape(aid)}/onboard-results"),
            timeout=10000,
        )
