"""Exclusive IA ownership: Ledger inbox vs Fleet scoreboard vs Admin Review.

See docs/portal-experience-design-language.md §1 and docs/ledger-design-spec.md
§3 exclusive-ownership tables.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


@pytest.fixture
async def ui_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.gates.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store), \
         patch("agentit.portal.routes.insights.get_store", return_value=store):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=True,
        ) as client:
            await prime_csrf(client)
            yield client, store


async def test_root_redirects_to_ledger(ui_client):
    client, _store = ui_client
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/ledger"


async def test_fleet_is_scoreboard_at_fleet_path(ui_client):
    client, store = ui_client
    await store.save(make_report(repo_name="scoreboard-app"))
    resp = await client.get("/fleet")
    assert resp.status_code == 200
    assert "<h1>Fleet</h1>" in resp.text
    assert "Portfolio scoreboard" in resp.text
    assert "Needs Action" not in resp.text
    assert "pending</a>" not in resp.text
    assert "scoreboard-app" in resp.text


async def test_ledger_is_ops_home_with_needs_you_default(ui_client):
    client, _store = ui_client
    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "<h1>Ledger</h1>" in resp.text
    assert "Morning inbox" in resp.text
    assert "Needs You" in resp.text


async def test_nav_needs_you_badge_on_ledger_not_fleet(ui_client):
    client, store = ui_client
    aid = await store.save(make_report(repo_name="nav-badge-app"))
    await store.create_gate(aid, "auto-mode-review", "needs review")
    with patch(
        "agentit.portal.helpers._nav_gate_badges_cache",
        {"pending_actions": 0, "admin_review": 0, "ts": 0.0},
    ):
        resp = await client.get("/ledger")
    assert resp.status_code == 200
    primary = resp.text.split('id="nav-primary"', 1)[1].split("links-secondary", 1)[0]
    assert 'href="/ledger"' in primary
    assert 'href="/fleet"' in primary
    assert 'Ledger\n      <span class="nav-badge">1</span>' in primary
    assert "Fleet\n      <span class=\"nav-badge\">" not in primary


async def test_fleet_quiet_pointer_to_ledger_needs_you(ui_client):
    client, store = ui_client
    aid = await store.save(make_report(repo_name="quiet-pointer-app"))
    await store.create_gate(aid, "auto-mode-review", "gate 1")
    await store.create_gate(aid, "dry-run-failed", "gate 2")
    resp = await client.get("/fleet")
    assert resp.status_code == 200
    assert 'href="/ledger"' in resp.text
    assert "2 need you → Ledger" in resp.text
    assert f'/assessments/{aid}?tab=actions' not in resp.text


async def test_admin_review_buried_in_menu_when_count_zero(ui_client):
    client, _store = ui_client
    with patch(
        "agentit.portal.helpers._nav_gate_badges_cache",
        {"pending_actions": 0, "admin_review": 0, "ts": 0.0},
    ):
        resp = await client.get("/ledger")
    assert resp.status_code == 200
    primary = resp.text.split('id="nav-primary"', 1)[1].split("links-secondary", 1)[0]
    assert "Admin Review" not in primary
    dropdown = resp.text.split("user-menu-dropdown", 1)[1]
    assert 'href="/admin-review"' in dropdown
    assert "Elevated approvals" in dropdown


async def test_admin_review_primary_nav_when_count_positive(ui_client):
    client, store = ui_client
    aid = await store.save(make_report(repo_name="admin-nav-app"))
    await store.create_gate(aid, "cluster-admin-review", "needs elevated review")
    with patch(
        "agentit.portal.helpers._nav_gate_badges_cache",
        {"pending_actions": 0, "admin_review": 0, "ts": 0.0},
    ):
        resp = await client.get("/ledger")
    assert resp.status_code == 200
    primary = resp.text.split('id="nav-primary"', 1)[1].split("links-secondary", 1)[0]
    assert 'Admin Review\n      <span class="nav-badge">1</span>' in primary
    dropdown = resp.text.split("user-menu-dropdown", 1)[1].split("deploy-status", 1)[0]
    assert "Elevated approvals" not in dropdown


async def test_admin_review_page_states_exclusive_job(ui_client):
    client, _store = ui_client
    resp = await client.get("/admin-review")
    assert resp.status_code == 200
    assert "<h1>Admin Review</h1>" in resp.text
    assert "Elevated approvals" in resp.text


async def test_events_page_does_not_claim_ops_home(ui_client):
    client, _store = ui_client
    resp = await client.get("/events")
    assert resp.status_code == 200
    assert "Ops home is" in resp.text
    assert 'href="/ledger"' in resp.text
