"""Tests for the double-submit-cookie CSRF protection (Part 2 of the
auth/CSRF/webhook-token hardening). See src/agentit/portal/csrf.py for the
full rationale.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentit.portal.app import app
from agentit.portal.store_factory import AsyncSQLiteStore
from conftest import make_store


@pytest.fixture(autouse=True)
def _override_store():
    test_store = make_store()
    async_store = AsyncSQLiteStore.wrap(test_store)
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store):
        yield test_store


@pytest.fixture
def client():
    return TestClient(app)


def test_get_sets_csrf_cookie(client):
    resp = client.get("/")
    assert "csrf_token" in resp.cookies
    assert len(resp.cookies["csrf_token"]) > 20


def test_post_without_any_csrf_token_rejected(client):
    """No cookie, no header/field at all -- the base case a real cross-site
    forged request would also produce."""
    resp = client.post("/settings/auto-mode", data={"value": "true"})
    assert resp.status_code == 403


def test_post_with_cookie_but_no_matching_header_rejected(client):
    """The victim's browser auto-sends the cookie, but a cross-origin
    attacker page can't read its value (same-origin policy) to also set a
    matching header -- this is exactly the attack double-submit defeats."""
    client.get("/")  # sets the cookie
    resp = client.post("/settings/auto-mode", data={"value": "true"})
    assert resp.status_code == 403


def test_post_with_mismatched_header_rejected(client):
    client.get("/")
    resp = client.post(
        "/settings/auto-mode", data={"value": "true"},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert resp.status_code == 403


def test_post_with_valid_double_submit_header_succeeds(client):
    get_resp = client.get("/")
    token = get_resp.cookies["csrf_token"]
    resp = client.post(
        "/settings/auto-mode", data={"value": "true"},
        headers={"X-CSRF-Token": token}, follow_redirects=False,
    )
    assert resp.status_code == 303


def test_post_with_valid_form_field_fallback_succeeds():
    """Non-JS/non-htmx submission path: the token as a hidden form field
    instead of a header (see csrf.py's get_submitted_token)."""
    client = TestClient(app)
    get_resp = client.get("/")
    token = get_resp.cookies["csrf_token"]
    resp = client.post(
        "/settings/auto-mode",
        data={"value": "true", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_webhook_routes_exempt_from_csrf(client):
    """Part 3 covers these with a separate shared-secret mechanism -- they're
    never browser form submissions and never carry the CSRF cookie."""
    resp = client.post("/api/webhook/assess", json={"criticality": "high"})
    # 400 (missing repo_url) proves we got *past* CSRF, not blocked by it.
    assert resp.status_code == 400


def test_healthz_and_readyz_exempt_from_csrf():
    """Not state-changing, and probed by kubelet/load balancers with no
    cookie -- must never require a CSRF token."""
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code in (200, 503)
