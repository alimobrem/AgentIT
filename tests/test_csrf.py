"""Tests for the double-submit-cookie CSRF protection (Part 2 of the
auth/CSRF/webhook-token hardening). See src/agentit/portal/csrf.py for the
full rationale.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_store


@pytest.fixture(autouse=True)
async def _override_store():
    test_store = await make_store()
    async_store = test_store
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=async_store):
        yield test_store


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True)


async def test_get_sets_csrf_cookie(client):
    # Hit a real HTML page (not `/`, which 302s to Ledger — redirect hops
    # don't expose Set-Cookie on the final response.cookies).
    resp = await client.get("/ledger")
    assert "csrf_token" in resp.cookies
    assert len(resp.cookies["csrf_token"]) > 20


async def test_post_without_any_csrf_token_rejected(client):
    """No cookie, no header/field at all -- the base case a real cross-site
    forged request would also produce."""
    resp = await client.post("/settings/purge", data={})
    assert resp.status_code == 403


async def test_post_with_cookie_but_no_matching_header_rejected(client):
    """The victim's browser auto-sends the cookie, but a cross-origin
    attacker page can't read its value (same-origin policy) to also set a
    matching header -- this is exactly the attack double-submit defeats."""
    await client.get("/ledger")  # sets the cookie
    resp = await client.post("/settings/purge", data={})
    assert resp.status_code == 403


async def test_post_with_mismatched_header_rejected(client):
    await client.get("/ledger")
    resp = await client.post(
        "/settings/purge", data={},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert resp.status_code == 403


async def test_post_with_valid_double_submit_header_succeeds(client):
    get_resp = await client.get("/ledger")
    token = get_resp.cookies["csrf_token"]
    resp = await client.post(
        "/settings/purge", data={},
        headers={"X-CSRF-Token": token}, follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_post_with_valid_form_field_fallback_succeeds():
    """Non-JS/non-htmx submission path: the token as a hidden form field
    instead of a header (see csrf.py's get_submitted_token)."""
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True)
    get_resp = await client.get("/ledger")
    token = get_resp.cookies["csrf_token"]
    resp = await client.post(
        "/settings/purge",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_webhook_routes_exempt_from_csrf(client):
    """Part 3 covers these with a separate shared-secret mechanism -- they're
    never browser form submissions and never carry the CSRF cookie."""
    resp = await client.post("/api/webhook/assess", json={"criticality": "high"})
    # 400 (missing repo_url) proves we got *past* CSRF, not blocked by it.
    assert resp.status_code == 400


async def test_healthz_and_readyz_exempt_from_csrf():
    """Not state-changing, and probed by kubelet/load balancers with no
    cookie -- must never require a CSRF token."""
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True)
    resp = await client.get("/healthz")
    assert resp.status_code in (200, 503)
