"""Ledger escalation enrichment: real why/how, never invent failure detail."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.assessment_diff import finding_key
from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.app import app
from agentit.portal.delivery import FINDING_ESCALATION_THRESHOLD, escalate_unresolved_finding
from agentit.portal.escalation_guidance import (
    _MISSING_WHY,
    enrich_escalation_event,
    parse_escalation_summary,
)
from conftest import make_report, make_store, prime_csrf

_NETWORK_TARGET = finding_key("network", "Missing NetworkPolicy")


@pytest.fixture()
async def guidance_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.helpers._store", store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=store), \
         patch("agentit.portal.routes.settings.get_store", return_value=store), \
         patch("agentit.portal.routes.insights.get_store", return_value=store), \
         patch("agentit.portal.routes.health.get_store", return_value=store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=store), \
         patch("agentit.portal.routes.slos.get_store", return_value=store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=store):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True,
        ) as client:
            await prime_csrf(client)
            yield client, store


def test_parse_escalation_summary_extracts_fields():
    summary = (
        "'network' finding has failed to resolve after 3 automated fix "
        "attempt(s) -- human review needed. Target finding: Missing NetworkPolicy"
    )
    parsed = parse_escalation_summary(summary)
    assert parsed["category"] == "network"
    assert parsed["attempt_count"] == 3
    assert parsed["finding_title"] == "Missing NetworkPolicy"


async def test_enrich_uses_stored_why_and_finding_recommendation():
    store = await make_store()
    report = make_report(repo_name="esc-guide-app")
    report.scores = [
        DimensionScore(
            dimension="security",
            score=40,
            max_score=100,
            findings=[
                Finding(
                    category="network",
                    severity=Severity.high,
                    description="Missing NetworkPolicy",
                    recommendation="Add a deny-by-default NetworkPolicy for this app.",
                    source="check:network",
                ),
            ],
        ),
    ]
    await store.log_event(
        "delivery-verifier",
        "delivery-finding-still-present",
        "esc-guide-app",
        "warning",
        "Delivery abc did NOT resolve: network ('Missing NetworkPolicy') still present on re-assessment",
    )
    event_id = await escalate_unresolved_finding(
        store, "aid", "esc-guide-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD,
    )
    events = await store.list_unresolved_events(
        "finding-escalated", ["finding-escalation-acknowledged"], target_app="esc-guide-app",
    )
    event = next(e for e in events if e["id"] == event_id)

    enriched = await enrich_escalation_event(store, event, report)

    assert enriched["category"] == "network"
    assert enriched["dimension"] == "security"
    assert enriched["attempt_count"] >= FINDING_ESCALATION_THRESHOLD
    assert "still present" in enriched["why_failed"]
    assert enriched["why_failed_recorded"] is True
    assert "NetworkPolicy" in enriched["how_to_fix_manually"]


async def test_enrich_honest_when_no_failure_detail():
    store = await make_store()
    event_id = await escalate_unresolved_finding(
        store, "aid", "esc-no-why", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD,
    )
    events = await store.list_unresolved_events(
        "finding-escalated", ["finding-escalation-acknowledged"], target_app="esc-no-why",
    )
    event = next(e for e in events if e["id"] == event_id)

    enriched = await enrich_escalation_event(store, dict(event), report=None)

    assert enriched["why_failed"] == _MISSING_WHY
    assert enriched["why_failed_recorded"] is False
    assert "NetworkPolicy" in enriched["how_to_fix_manually"] or "network" in enriched["how_to_fix_manually"].lower()


async def test_ledger_card_renders_why_and_how(guidance_client):
    client, store = guidance_client
    report = make_report(repo_name="esc-ui-app")
    report.scores = [
        DimensionScore(
            dimension="security",
            score=40,
            max_score=100,
            findings=[
                Finding(
                    category="network",
                    severity=Severity.high,
                    description="Missing NetworkPolicy",
                    recommendation="Add a NetworkPolicy restricting ingress.",
                    source="check:network",
                ),
            ],
        ),
    ]
    aid = await store.save(report)
    await store.save_onboarding(
        aid, [{"category": "security", "path": "x.yaml", "content": "a: b", "description": "d"}],
    )
    await store.log_event(
        "delivery-verifier",
        "finding-redispatch-no-fix",
        "esc-ui-app",
        "warning",
        "Re-dispatch for 'network' produced no fix: dispatcher produced no files",
    )
    await escalate_unresolved_finding(
        store, aid, "esc-ui-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD,
    )

    text = (await client.get(f"/assessments/{aid}?tab=ledger")).text

    assert "Escalated finding" in text
    assert "Why we couldn" in text or "Why we couldn&rsquo;t fix" in text
    assert "How to fix manually" in text
    assert "dispatcher produced no files" in text or "produced no fix" in text
    assert "NetworkPolicy" in text
    assert "Acknowledge" in text
    # Acknowledge is secondary (outline sm), not a primary danger/action CTA.
    assert "btn-outline btn-sm" in text
