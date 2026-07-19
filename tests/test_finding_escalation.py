"""Tests for Phase 4 of docs/onboarding-loop-vision-gap-analysis.md's
bounded auto-escalation: below a confirmed-failure threshold, re-dispatch a
fresh fix through the exact mechanism the original delivery used; at or
above it, stop retrying and create a real, visible escalation gate instead.

Covers:
- `store.get_finding_failure_count()`'s counting shape.
- `delivery.handle_confirmed_finding_failure()`'s below/at-threshold branch
  choice.
- `delivery.escalate_unresolved_finding()` creates a real, visible
  `finding-unresolved-escalation` gate (Ledger card D, Fleet's "needs
  action" badge) rather than a silent give-up.
- The full repeated-failure loop through `check_pending_delivery_
  verifications()`: confirmed failures count up correctly, escalation
  fires exactly at the threshold (not before), and the loop then stops --
  no further auto-retry, no unbounded delivery growth.
- `resolve_gate()`'s special case: approving the escalation gate
  acknowledges it without re-delivering the whole onboarding batch.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.assessment_diff import finding_key
from agentit.ledger import get_ledger_cards
from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.delivery import (
    ESCALATION_GATE_TYPE,
    FINDING_ESCALATION_THRESHOLD,
    MECHANISM_INFRA_REPO_COMMIT,
    check_pending_delivery_verifications,
    escalate_unresolved_finding,
    handle_confirmed_finding_failure,
)
from conftest import make_async_store, make_report

_NETWORK_TARGET = finding_key("network", "Missing NetworkPolicy")


def _report_with_network_finding(**kwargs):
    report = make_report(**kwargs)
    report.scores = [
        DimensionScore(
            dimension="security", score=60, max_score=100,
            findings=[Finding(category="network", severity=Severity.medium,
                               description="Missing NetworkPolicy", recommendation="Add one")],
        ),
    ]
    report.overall_score = 60
    return report


def _push_body(repo_url: str) -> dict:
    return {
        "ref": "refs/heads/main",
        "repository": {"html_url": repo_url, "default_branch": "main"},
        "pusher": {"name": "tester"},
        "after": "abcdef012345",
        "commits": [],
    }


def _gitops_patches():
    """The same boundary mocks test_webhook_autofix_dispatch.py uses so a
    GitOps-registered redispatch delivery never makes a real cluster/GitHub
    call: the delivery mechanism resolves to infra-repo-commit and the
    commit itself is mocked out."""
    return (
        patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}),
        patch("agentit.portal.github_pr.commit_to_infra_repo",
              return_value={"pr_url": "https://github.com/org/infra-gitops/pull/1",
                            "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}),
        patch("agentit.portal.github_pr.ensure_applicationset"),
    )


class TestGetFindingFailureCount:
    async def test_zero_before_any_confirmed_failure(self):
        store, _ = await make_async_store()
        assert await store.get_finding_failure_count("esc-app", "network") == 0

    async def test_counts_only_still_present_deliveries_for_this_category(self):
        store, _ = await make_async_store()
        aid = await store.save(_report_with_network_finding(repo_name="esc-app"))

        still_present_id = await store.create_delivery(
            aid, "esc-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(still_present_id, finding_resolution="still_present")

        resolved_id = await store.create_delivery(
            aid, "esc-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(resolved_id, finding_resolution="resolved")

        other_category_id = await store.create_delivery(
            aid, "esc-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[finding_key("container", "Root user in Containerfile")],
        )
        await store.update_delivery(other_category_id, finding_resolution="still_present")

        assert await store.get_finding_failure_count("esc-app", "network") == 1
        assert await store.get_finding_failure_count("esc-app", "container") == 1
        assert await store.get_finding_failure_count("other-app", "network") == 0


class TestHandleConfirmedFindingFailure:
    async def test_below_threshold_redispatches_not_escalates(self):
        store, _ = await make_async_store()
        report = _report_with_network_finding(repo_name="esc-app2")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await store.save(report)

        patches = _gitops_patches()
        with patches[0], patches[1] as mock_commit, patches[2]:
            result = await handle_confirmed_finding_failure(
                store, report, aid, "esc-app2", _NETWORK_TARGET,
            )

        assert result["action"] == "redispatched"
        assert result["failure_count"] == 0  # no confirmed failures existed yet
        mock_commit.assert_called_once()

        gates = await store.list_gates(status="pending")
        assert not any(g["gate_type"] == ESCALATION_GATE_TYPE for g in gates)

        # The re-dispatch's own delivery is real and itself carries the
        # same target finding, so a later push can correlate IT too.
        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert [tuple(t) for t in deliveries[0]["target_findings"]] == [_NETWORK_TARGET]

    async def test_at_threshold_escalates_not_redispatches(self):
        store, _ = await make_async_store()
        report = _report_with_network_finding(repo_name="esc-app3")
        aid = await store.save(report)

        # Pre-seed FINDING_ESCALATION_THRESHOLD confirmed prior failures.
        for _ in range(FINDING_ESCALATION_THRESHOLD):
            did = await store.create_delivery(
                aid, "esc-app3", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
                target_findings=[_NETWORK_TARGET],
            )
            await store.update_delivery(did, finding_resolution="still_present")

        with patch("agentit.remediation.dispatcher.RemediationDispatcher.dispatch") as mock_dispatch:
            result = await handle_confirmed_finding_failure(
                store, report, aid, "esc-app3", _NETWORK_TARGET,
            )

        assert result["action"] == "escalated"
        assert result["failure_count"] == FINDING_ESCALATION_THRESHOLD
        mock_dispatch.assert_not_called()

        gates = await store.list_gates(status="pending")
        matching = [g for g in gates if g["gate_type"] == ESCALATION_GATE_TYPE]
        assert len(matching) == 1
        assert str(FINDING_ESCALATION_THRESHOLD) in matching[0]["summary"]

        # No new delivery was created by escalating -- the loop genuinely
        # stops, it doesn't keep generating (and failing) fixes forever.
        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == FINDING_ESCALATION_THRESHOLD

    async def test_escalation_gate_is_a_real_visible_ledger_signal(self):
        store, _ = await make_async_store()
        aid = await store.save(_report_with_network_finding(repo_name="esc-app4"))

        gate_id = await escalate_unresolved_finding(store, aid, "esc-app4", _NETWORK_TARGET, 3)

        cards = await get_ledger_cards(store, target_app="esc-app4")
        gate_cards = [c for c in cards if c["card_type"] == "D"]
        assert any(c["raw"]["id"] == gate_id for c in gate_cards)

        event_cards = [c for c in cards if c["title"] == "finding-escalated"]
        assert len(event_cards) == 1
        assert event_cards[0]["card_type"] == "I"


class TestRepeatedFailureLoopStopsAtThreshold:
    async def test_escalation_fires_at_threshold_not_before(self):
        """The core Phase 4 guarantee: repeated confirmed failures for the
        same (app, category) count up correctly across separate
        `check_pending_delivery_verifications()` calls (one per simulated
        push), auto-retrying below the threshold and escalating -- exactly
        once, never before -- at it. After escalation, no further delivery
        is created for this finding, so the loop provably stops instead of
        regenerating an identical fix forever."""
        store, _ = await make_async_store()
        app_name = "esc-app5"
        report = _report_with_network_finding(repo_name=app_name)
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await store.save(report)

        # Seed the very first delivery by hand (as if a prior webhook
        # auto-fix dispatch already ran and delivered it).
        first_delivery_id = await store.create_delivery(
            aid, app_name, {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[_NETWORK_TARGET],
        )

        patches = _gitops_patches()
        escalated_at = None
        with patches[0], patches[1] as mock_commit, patches[2]:
            for attempt in range(1, FINDING_ESCALATION_THRESHOLD + 2):
                # Each "push" re-assesses and finds the SAME finding, unchanged.
                new_report = _report_with_network_finding(repo_name=app_name)
                new_aid = await store.save(new_report)

                results = await check_pending_delivery_verifications(store, app_name, new_report, new_aid)
                assert len(results) == 1
                assert results[0]["status"] == "still_present"
                escalation_actions = [e["action"] for e in results[0]["escalations"]]

                if attempt < FINDING_ESCALATION_THRESHOLD:
                    assert escalation_actions == ["redispatched"], f"attempt {attempt}"
                else:
                    assert escalation_actions == ["escalated"], f"attempt {attempt}"
                    escalated_at = attempt
                    break

        assert escalated_at == FINDING_ESCALATION_THRESHOLD

        gates = await store.list_gates(status="pending")
        escalation_gates = [g for g in gates if g["gate_type"] == ESCALATION_GATE_TYPE]
        assert len(escalation_gates) == 1

        # Every delivery attempt up to (and including) the one that
        # triggered escalation is real and accounted for: the seeded one
        # plus one re-dispatched delivery per below-threshold attempt --
        # never more (escalating creates a gate, not another delivery).
        all_deliveries = await store.list_all_deliveries(limit=200)
        target_deliveries = {
            d["id"] for d in all_deliveries
            if d["app_name"] == app_name and [tuple(t) for t in d["target_findings"]] == [_NETWORK_TARGET]
        }
        assert first_delivery_id in target_deliveries
        # threshold-1 redispatched deliveries + the original seeded one.
        assert len(target_deliveries) == FINDING_ESCALATION_THRESHOLD

        # One more push after escalation: no further redispatch/escalation
        # attempt fires (nothing left in the pending-check queue for this
        # finding -- the escalated delivery's own finding_resolution is
        # already set, and escalating created no new delivery to re-check).
        mock_commit.reset_mock()
        final_report = _report_with_network_finding(repo_name=app_name)
        final_aid = await store.save(final_report)
        with patches[0], patches[2]:
            further_results = await check_pending_delivery_verifications(store, app_name, final_report, final_aid)
        assert further_results == []
        mock_commit.assert_not_called()


class TestEscalationGateResolution:
    async def test_approving_escalation_gate_does_not_redeliver_whole_batch(self, portal_client):
        """Approving a finding-unresolved-escalation gate must be a pure
        acknowledgment -- never a fallthrough to the generic gate-approve
        branch, which would re-deliver this app's ENTIRE onboarding batch
        rather than just this one escalated finding."""
        client, store, aid = portal_client
        report = await store.get(aid)

        gate_id = await escalate_unresolved_finding(store, aid, report.repo_name, _NETWORK_TARGET, 3)

        with patch("agentit.portal.routes.gates.route_and_deliver") as mock_route_and_deliver:
            resp = await client.post(
                f"/gates/{gate_id}/resolve",
                data={"status": "approved"},
            )

        assert resp.status_code in (200, 303)
        mock_route_and_deliver.assert_not_called()

        gates = await store.list_gates(status="approved")
        assert any(g["id"] == gate_id for g in gates)
