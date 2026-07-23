"""Tests for Phase 5 of docs/onboarding-loop-vision-gap-analysis.md's Step 8
discussion: `delivery.get_next_action_state()`, the backend helper that
answers, per app, a real "what happens next" fact -- built entirely from
Phase 3/4's own data (``deliveries.target_findings_json``/
``finding_resolution``, ``store.get_finding_failure_count()``, an unresolved
``finding-escalated`` event) -- never a fabricated re-check cadence.

Covers the four states (escalated, retrying, pending_verification, none) and
the priority ordering when more than one could apply for the same app. See
``tests/test_next_action_state_ui.py`` for how these states render on Fleet
and Assessment Detail.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.assessment_diff import finding_key
from agentit.portal.delivery import (
    FINDING_ESCALATION_THRESHOLD,
    MECHANISM_INFRA_REPO_COMMIT,
    NEXT_ACTION_ESCALATED,
    NEXT_ACTION_NONE,
    NEXT_ACTION_PENDING_VERIFICATION,
    NEXT_ACTION_RETRYING,
    escalate_unresolved_finding,
    get_next_action_state,
)
from conftest import make_async_store, make_report

_NETWORK_TARGET = finding_key("network", "Missing NetworkPolicy")
_CONTAINER_TARGET = finding_key("container", "Root user in Containerfile")


class TestGetNextActionState:
    async def test_none_when_nothing_pending_or_failing(self):
        store, _ = await make_async_store()
        await store.save(make_report(repo_name="clean-app"))

        state = await get_next_action_state(store, "clean-app")

        assert state["state"] == NEXT_ACTION_NONE
        # Must not imply a periodic schedule that doesn't exist.
        assert "push" in state["message"] or "re-Assess" in state["message"]
        assert "schedule" in state["message"]

    async def test_pending_verification_for_a_fresh_target_finding(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="pend-app", repo_url="https://github.com/org/pend-app"))
        await store.create_delivery(
            aid, "pend-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        state = await get_next_action_state(store, "pend-app", repo_url="https://github.com/org/pend-app")

        assert state["state"] == NEXT_ACTION_PENDING_VERIFICATION
        assert "https://github.com/org/pend-app" in state["message"]

    async def test_pending_verification_falls_back_to_app_name_without_repo_url(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="pend-app2"))
        await store.create_delivery(
            aid, "pend-app2", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        state = await get_next_action_state(store, "pend-app2")

        assert state["state"] == NEXT_ACTION_PENDING_VERIFICATION
        assert "pend-app2" in state["message"]

    async def test_retrying_once_a_finding_has_already_failed_once(self):
        """The exact shape a real bounded auto-retry leaves behind
        (``handle_confirmed_finding_failure()``'s below-threshold branch):
        the failed delivery's own row now has ``finding_resolution=
        "still_present"``, and the redispatch it triggered created a fresh,
        not-yet-checked delivery targeting the same finding."""
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="retry-app"))
        first_id = await store.create_delivery(
            aid, "retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(first_id, finding_resolution="still_present")
        await store.create_delivery(
            aid, "retry-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )

        state = await get_next_action_state(store, "retry-app")

        assert state["state"] == NEXT_ACTION_RETRYING
        assert state["failure_count"] == 1
        assert state["category"] == "network"
        assert f"Retry 1 of {FINDING_ESCALATION_THRESHOLD}" in state["message"]

    async def test_retrying_reports_the_worst_failure_count_across_findings(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="retry-app2"))

        # "network" has failed once already; "container" has never failed.
        network_first = await store.create_delivery(
            aid, "retry-app2", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )
        await store.update_delivery(network_first, finding_resolution="still_present")
        await store.create_delivery(
            aid, "retry-app2", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_NETWORK_TARGET],
        )
        await store.create_delivery(
            aid, "retry-app2", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_CONTAINER_TARGET],
        )

        state = await get_next_action_state(store, "retry-app2")

        assert state["state"] == NEXT_ACTION_RETRYING
        assert state["category"] == "network"
        assert state["failure_count"] == 1

    async def test_escalated_when_an_unresolved_escalation_event_exists(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="esc-app"))

        event_id = await escalate_unresolved_finding(
            store, aid, "esc-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD,
        )

        state = await get_next_action_state(store, "esc-app")

        assert state["state"] == NEXT_ACTION_ESCALATED
        assert state["event_id"] == event_id
        assert state["category"] == "network"
        assert "network" in state["message"]
        assert "Needs your review" in state["message"]

    async def test_escalated_state_embeds_the_given_assessment_id(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="esc-app-aid"))
        await escalate_unresolved_finding(store, aid, "esc-app-aid", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)

        state = await get_next_action_state(store, "esc-app-aid", assessment_id=aid)

        assert state["assessment_id"] == aid

    async def test_escalation_takes_priority_over_an_unrelated_pending_delivery(self):
        """An app can have one finding already escalated (needs a human)
        and a completely different finding still just pending its first
        verification at the same time -- the single "next action" fact must
        surface the more urgent one."""
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="mixed-app"))
        await escalate_unresolved_finding(store, aid, "mixed-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)
        await store.create_delivery(
            aid, "mixed-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}}},
            target_findings=[_CONTAINER_TARGET],
        )

        state = await get_next_action_state(store, "mixed-app")

        assert state["state"] == NEXT_ACTION_ESCALATED

    async def test_escalation_for_a_different_app_is_ignored(self):
        store, _ = await make_async_store()
        other_aid = await store.save(make_report(repo_name="other-app"))
        await escalate_unresolved_finding(store, other_aid, "other-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)
        await store.save(make_report(repo_name="innocent-app"))

        state = await get_next_action_state(store, "innocent-app")

        assert state["state"] == NEXT_ACTION_NONE

    async def test_acknowledged_escalation_no_longer_counts(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="ack-app"))
        event_id = await escalate_unresolved_finding(store, aid, "ack-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD)
        await store.log_event(
            "human", "finding-escalation-acknowledged", "ack-app", "info", "acknowledged", correlation_id=event_id,
        )

        state = await get_next_action_state(store, "ack-app")

        assert state["state"] == NEXT_ACTION_NONE

    async def test_reuses_a_pre_fetched_unresolved_escalations_list_without_a_new_query(self):
        """Fleet enrichment fetches ``list_unresolved_events(...)`` once for
        the whole page -- passing it in as ``unresolved_escalations`` must
        skip this function's own query entirely, not issue a second one."""
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="prefetch-app"))
        event_id = await escalate_unresolved_finding(
            store, aid, "prefetch-app", _NETWORK_TARGET, FINDING_ESCALATION_THRESHOLD,
        )
        unresolved_escalations = await store.list_unresolved_events(
            "finding-escalated", ["finding-escalation-acknowledged"],
        )

        with patch.object(
            store, "list_unresolved_events",
            side_effect=AssertionError("list_unresolved_events should not be called again"),
        ):
            state = await get_next_action_state(
                store, "prefetch-app", unresolved_escalations=unresolved_escalations,
            )

        assert state["state"] == NEXT_ACTION_ESCALATED
        assert state["event_id"] == event_id
