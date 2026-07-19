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
         patch("agentit.portal.routes.gates.get_store", return_value=store), \
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
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(first_id, finding_resolution="still_present")
        await store.create_delivery(
            aid, "fleet-retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[_NETWORK_TARGET],
        )

        text = (await client.get("/fleet")).text

        assert f"Retry 1 of {FINDING_ESCALATION_THRESHOLD}" in text
        assert "badge-warning" in text

    async def test_escalated_badge_links_to_actions_tab(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="fleet-esc-app"))
        await escalate_unresolved_finding(store, aid, "fleet-esc-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)

        text = (await client.get("/fleet")).text

        assert "Needs review" in text
        assert "badge-accent" in text
        assert f'/assessments/{aid}?tab=actions' in text

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
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(first_id, finding_resolution="still_present")
        await store.create_delivery(
            aid, "ad-retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[_NETWORK_TARGET],
        )

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Next action:" in text
        assert f"Retry 1 of {FINDING_ESCALATION_THRESHOLD}" in text
        assert "alert-warn" in text

    async def test_escalated_message_links_to_actions_and_ledger(self, next_action_client):
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-esc-app"))
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])
        gate_id = await escalate_unresolved_finding(store, aid, "ad-esc-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Next action:" in text
        assert "Needs your review" in text
        assert "automated fixes exhausted for" in text and "network" in text
        assert "alert-error" in text
        assert f"/assessments/{aid}?tab=actions" in text
        assert f"/assessments/{aid}?tab=ledger" in text
        # Confirm the escalation gate this message points to is the real one.
        gates = await store.list_gates(status="pending")
        assert any(g["id"] == gate_id for g in gates)

    async def test_honest_no_schedule_message_for_a_previously_onboarded_clean_app(self, next_action_client):
        """A previously-onboarded app with nothing currently pending or
        failing gets the honest "no scheduled re-check" fact, not silence
        and not a fabricated cadence."""
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-clean-onboarded-app"))
        await store.save_onboarding(aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}])

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Next action:" not in text  # only the "something's happening" states use this label
        assert "no periodic re-check on a schedule" in text
        assert "next push" in text

    async def test_no_next_action_noise_for_a_brand_new_never_onboarded_app(self, next_action_client):
        """A fresh, never-onboarded app's only real next step is "Onboard
        This App" (the existing lifecycle hint) -- restating "nothing
        pending" underneath it would just be noise."""
        client, store = next_action_client
        aid = await store.save(make_report(repo_name="ad-fresh-app"))

        text = (await client.get(f"/assessments/{aid}")).text

        assert "Onboard This App" in text
        assert "Next action:" not in text
        assert "no periodic re-check on a schedule" not in text
