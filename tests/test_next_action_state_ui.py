"""Tests for Phase 5 of docs/onboarding-loop-vision-gap-analysis.md's Step 8
discussion: the two UI surfaces built on top of `delivery.get_next_action_
state()` (see ``tests/test_next_action_state.py`` for the backend helper's
own tests) -- Fleet's compact per-app badge and Assessment Detail's more
detailed header-area indicator.

Covers:
- Fleet's per-app indicator (``fleet.py::_attach_next_action_state()``):
  renders for the three "something's happening" states, omits entirely for
  a genuinely clean app.
- Assessment Detail's header-area indicator: same three states, plus the
  honest "nothing pending, no scheduled re-check" text for a previously-
  onboarded, currently clean app -- and its deliberate suppression for a
  brand-new, never-onboarded app (whose only real next step is "Onboard
  This App", already covered by the existing lifecycle hint).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.assessment_diff import finding_key
from agentit.portal.app import app
from agentit.portal.delivery import (
    FINDING_ESCALATION_THRESHOLD,
    MECHANISM_INFRA_REPO_COMMIT,
    escalate_unresolved_finding,
)
from conftest import make_report, make_store, prime_csrf

_NETWORK_TARGET = finding_key("network", "Missing NetworkPolicy")


@pytest.fixture()
async def next_action_client():
    """Async HTTP client, real store, all `get_store()` call sites patched
    to the same store instance -- mirrors `conftest.py::portal_client` /
    `test_multi_app_fleet.py::fleet_client`'s exact pattern, but seeds no
    app of its own so each test controls its own app(s) and state(s).
    """
    store = await make_store()

    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.helpers._store", store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=store), \
         patch("agentit.portal.routes.health.get_store", return_value=store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=store), \
         patch("agentit.portal.routes.settings.get_store", return_value=store), \
         patch("agentit.portal.routes.insights.get_store", return_value=store), \
         patch("agentit.portal.routes.slos.get_store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store


class TestFleetNextActionIndicator:
    async def test_pending_verification_badge(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="fleet-pend-app", repo_url="https://github.com/org/fleet-pend-app"))
        await store.create_delivery(
            aid, "fleet-pend-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        text = (await client.get("/fleet")).text

        assert "Awaiting verification" in text
        assert "badge-info" in text

    async def test_retrying_badge(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="fleet-retry-app"))
        first_id = await store.create_delivery(
            aid, "fleet-retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(first_id, finding_resolution="still_present")
        await store.create_delivery(
            aid, "fleet-retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        text = (await client.get("/fleet")).text

        assert f"Retry 1 of {FINDING_ESCALATION_THRESHOLD}" in text
        assert "badge-warning" in text

    async def test_escalated_badge_links_to_ledger_tab(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="fleet-esc-app"))
        await escalate_unresolved_finding(store, aid, "fleet-esc-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)

        text = (await client.get("/fleet")).text

        assert "Needs review" in text
        assert "badge-accent" in text
        assert f'/assessments/{aid}?tab=ledger' in text

    async def test_clean_app_omits_the_indicator_entirely(self, next_action_client):
        client, store = next_action_client
        await store.save(make_report(repo_name="fleet-clean-app"))

        text = (await client.get("/fleet")).text

        assert "fleet-clean-app" in text
        assert "Awaiting verification" not in text
        assert "Needs review" not in text
        assert "Retry 1 of" not in text


class TestAssessmentDetailNextActionIndicator:
    async def test_pending_verification_message(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-pend-app", repo_url="https://github.com/org/ad-pend-app"))
        # Pushes lifecycle_stage past "assessed" so the indicator is shown
        # unconditionally (see `_next_action_relevant` in the template).
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])
        await store.create_delivery(
            aid, "ad-pend-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Next action:" in text
        assert "Awaiting verification" in text
        assert "https://github.com/org/ad-pend-app" in text
        assert "alert-info" in text

    async def test_retrying_message(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-retry-app"))
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])
        first_id = await store.create_delivery(
            aid, "ad-retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(first_id, finding_resolution="still_present")
        await store.create_delivery(
            aid, "ad-retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Next action:" in text
        assert f"Retry 1 of {FINDING_ESCALATION_THRESHOLD}" in text
        assert "alert-warn" in text

    async def test_escalated_message_links_to_ledger(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-esc-app"))
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])
        event_id = await escalate_unresolved_finding(store, aid, "ad-esc-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Next action:" in text
        assert "Needs your review" in text
        assert "automated fixes exhausted for" in text and "network" in text
        assert "alert-error" in text
        # Former Actions + Ledger tabs merged into one -- a single link now,
        # not two separate ones pointing at the same underlying gate.
        assert f"/assessments/{aid}?tab=ledger" in text
        assert "See how to fix on the Ledger tab" in text
        assert "Review on the Ledger tab" not in text
        # Confirm the escalation event this message points to is the real one, and unresolved.
        unresolved = await store.list_unresolved_events(
            "finding-escalated", ["finding-escalation-acknowledged"], target_app="ad-esc-app",
        )
        assert any(e["id"] == event_id for e in unresolved)

    async def test_open_prs_win_over_escalated_next_action(self, next_action_client):
        """When open PRs exist, merge on GitHub is the next action — not Ledger."""
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-pr-over-esc", repo_url="https://github.com/org/ad-pr-over-esc"))
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])
        await escalate_unresolved_finding(
            store, aid, "ad-pr-over-esc", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD,
        )
        pr_url = "https://github.com/org/ad-pr-over-esc/pull/9"
        await store.create_delivery(
            aid, "ad-pr-over-esc", {"cluster_config": 1},
            mechanism="cluster_config:infra-repo-commit", status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )

        with patch("agentit.portal.github_pr.get_pr_status",
                    return_value={"state": "open", "html_url": pr_url, "title": "onboard", "merged_at": ""}):
            text = (await client.get(f"/assessments/{aid}")).text

        hint = text.split('class="next-step-hint', 1)[1].split("</div>", 1)[0]
        assert "open PR" in hint
        assert "merge on GitHub" in hint
        assert "Review on the Ledger tab" not in hint
        # Escalated next-action copy must not compete when PRs are waiting.
        assert "automated fixes exhausted" not in hint
        assert pr_url in text

    async def test_honest_no_schedule_message_for_a_previously_onboarded_clean_app(self, next_action_client):
        """A previously-onboarded clean app (no remediable findings) does not
        promise mergeable PRs or nudge Scan for a PR — single quiet banner."""
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-clean-onboarded-app"))
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])

        text = (await client.get(f"/assessments/{aid}")).text

        assert "no remediable findings to open a PR for right now" in text
        assert "No open PRs — nothing remediable to open one for right now." in text
        assert "when pull request(s) appear below" not in text
        # No competing "Next action:" escalation/retry banner on a clean app.
        assert "automated fixes exhausted" not in text
        assert "Awaiting verification" not in text

    async def test_no_next_action_noise_for_a_brand_new_never_onboarded_app(self, next_action_client):
        """A fresh, never-onboarded app's only real next step is Scan (the
        existing lifecycle hint, which always chains into onboarding
        automatically) -- restating "nothing pending" underneath it would
        just be noise."""
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-fresh-app"))

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Onboard This App" not in text
        assert "Next action:" not in text
        assert "no periodic re-check on a schedule" not in text
