"""Exclusive IA ownership: Ledger inbox vs Fleet scoreboard. (Admin Review, a
third, elevated-approvals nav item, was retired 2026-07-18 along with the
`cluster-admin-review` gate type it existed solely for -- see
delivery.py/routes/gates.py.)

See docs/portal-experience-design-language.md §1 and docs/ledger-design-spec.md
§3 exclusive-ownership tables.
"""
from __future__ import annotations

import re
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
        {"pending_actions": 0, "ts": 0.0},
    ):
        resp = await client.get("/ledger")
    assert resp.status_code == 200
    primary = resp.text.split('id="nav-primary"', 1)[1].split("links-secondary", 1)[0]
    assert 'href="/ledger"' in primary
    assert 'href="/fleet"' in primary
    assert re.search(r'Ledger\s*<span class="nav-badge">1</span>', primary)
    assert not re.search(r'Fleet\s*<span class="nav-badge">', primary)


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


async def test_admin_review_nav_and_page_are_gone(ui_client):
    """Admin Review (nav link, account-menu entry, and page) was retired
    2026-07-18 along with the `cluster-admin-review` gate type it existed
    solely for -- every gate type is per-app now, so there's no cross-app
    elevated-approvals queue left to link to, even with a pending gate of
    that (now-legacy) type in the fleet."""
    client, store = ui_client
    aid = await store.save(make_report(repo_name="admin-nav-app"))
    await store.create_gate(aid, "cluster-admin-review", "needs elevated review")
    with patch(
        "agentit.portal.helpers._nav_gate_badges_cache",
        {"pending_actions": 0, "ts": 0.0},
    ):
        resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "Admin Review" not in resp.text
    assert 'href="/admin-review"' not in resp.text

    page_resp = await client.get("/admin-review", follow_redirects=False)
    assert page_resp.status_code == 404


async def test_events_page_does_not_claim_ops_home(ui_client):
    client, _store = ui_client
    resp = await client.get("/events")
    assert resp.status_code == 200
    assert "Ops home is" in resp.text
    assert 'href="/ledger"' in resp.text
