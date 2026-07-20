"""Exclusive IA ownership: Ledger's PR list vs Fleet scoreboard. (Admin
Review, a third, elevated-approvals nav item, was retired 2026-07-18 along
with the `cluster-admin-review` gate type it existed solely for -- see
delivery.py/routes/gates.py.)

See docs/portal-experience-design-language.md §1. Ledger's own job was
redefined by product direction (docs/ledger-design-spec.md's original A-P
generic event union is superseded): it's now strictly a fleet-wide PR list/
lifecycle view (waiting for approval / open / merged / rejected / closed),
not a generic "everything that needs a human" inbox -- non-PR gate types
(``auto-mode-review``, ``rollback-review``, ``finding-unresolved-
escalation``) never show up there; they stay on Fleet's per-app badges and
Assessment Detail's own Ledger tab (formerly Actions -- merged 2026-07-19).
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


async def test_ledger_is_the_pr_list_not_a_generic_inbox(ui_client):
    client, _store = ui_client
    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "<h1>Ledger</h1>" in resp.text
    assert "Waiting for your approval" in resp.text
    assert "PR history" in resp.text


async def test_ledger_never_shows_non_pr_gates(ui_client):
    """auto-mode-review is a real, pending, app-owner gate -- but not a PR
    gate -- so it must never appear in Ledger's "Waiting for your approval"
    list (it belongs on Fleet's needs-action badge / Assessment Detail's
    own Ledger tab instead)."""
    client, store = ui_client
    aid = await store.save(make_report(repo_name="non-pr-gate-app"))
    await store.create_gate(aid, "auto-mode-review", "needs review")
    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "Waiting for your approval (0)" in resp.text


async def test_nav_needs_you_badge_on_ledger_reflects_pr_gates_only(ui_client):
    client, store = ui_client
    aid = await store.save(make_report(repo_name="nav-badge-app"))
    # A non-PR pending gate must NOT move this badge -- only a pending
    # gitops-pr-pending gate (a PR waiting for approval) does.
    await store.create_gate(aid, "auto-mode-review", "needs review")
    await store.create_gate(
        aid, "gitops-pr-pending",
        "PR opened: https://github.com/org/nav-badge-app-gitops/pull/1. Approving this gate merges the PR.",
        pr_url="https://github.com/org/nav-badge-app-gitops/pull/1",
    )
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


async def test_fleet_quiet_pointer_to_ledger_counts_pr_gates_only(ui_client):
    client, store = ui_client
    aid = await store.save(make_report(repo_name="quiet-pointer-app"))
    # Two non-PR gates must not inflate the "N PR(s) need your approval"
    # banner -- only the one real gitops-pr-pending gate below does.
    await store.create_gate(aid, "auto-mode-review", "gate 1")
    await store.create_gate(aid, "dry-run-failed", "gate 2")
    await store.create_gate(
        aid, "gitops-pr-pending",
        "PR opened: https://github.com/org/quiet-pointer-app-gitops/pull/1. Approving this gate merges the PR.",
        pr_url="https://github.com/org/quiet-pointer-app-gitops/pull/1",
    )
    with patch(
        "agentit.portal.helpers._nav_gate_badges_cache",
        {"pending_actions": 0, "ts": 0.0},
    ):
        resp = await client.get("/fleet")
    assert resp.status_code == 200, resp.text[:500]
    assert 'href="/ledger"' in resp.text
    assert "1 PR(s) need your approval → Ledger" in resp.text
    # The two non-PR gates are real pending actions too -- they show via
    # this app's own row badge (linking straight to its Ledger tab)
    # instead of inflating the PR-specific fleet-wide pointer above.
    assert f'/assessments/{aid}?tab=ledger' in resp.text
    assert "3 pending action" in resp.text


async def test_fleet_pointer_and_nav_badge_also_count_gateless_open_prs(ui_client):
    """2026-07-19 fix: a source-repo-pr delivery PR never gets an in-app
    gate row at all -- both the Fleet pointer banner and the nav badge
    must still count it as "waiting for your approval" (the same
    PR-status-derived definition Ledger's own stat now uses), not just
    gate-tracked PRs."""
    client, store = ui_client
    aid = await store.save(make_report(repo_name="gateless-pointer-app"))
    report = await store.get(aid)
    pr_url = "https://github.com/org/gateless-pointer-app/pull/1"
    await store.create_delivery(
        aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
        status="delivered",
        details={"outcomes": {"source_patch": {"pr_url": pr_url}}},
    )
    with patch(
        "agentit.portal.helpers._nav_gate_badges_cache",
        {"pending_actions": 0, "ts": 0.0},
    ), patch(
        "agentit.portal.github_pr.get_pr_status",
        return_value={"state": "open", "html_url": pr_url, "title": "fix", "merged_at": ""},
    ):
        fleet_resp = await client.get("/fleet")
        ledger_resp = await client.get("/ledger")
    assert fleet_resp.status_code == 200
    assert "1 PR(s) need your approval → Ledger" in fleet_resp.text
    assert "Waiting for your approval (1)" in ledger_resp.text
    primary = ledger_resp.text.split('id="nav-primary"', 1)[1].split("links-secondary", 1)[0]
    assert re.search(r'Ledger\s*<span class="nav-badge">1</span>', primary)


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
    """Events is the system-activity/audit-trail feed (every action the
    system takes, behind the scenes) -- it must not claim to be the
    primary destination for something that needs a human's attention,
    and must point at Ledger for that instead."""
    client, _store = ui_client
    resp = await client.get("/events")
    assert resp.status_code == 200
    assert "Every action the system takes" in resp.text
    assert 'href="/ledger"' in resp.text
