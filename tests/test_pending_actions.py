"""``portal/pending_actions.py`` -- the shared wrapper around
``store.list_unresolved_events()`` for the rollback-recommendation +
finding-escalation pair, used consistently by every "what's still waiting
on a human" call site (``routes/assessments.py``, ``routes/fleet.py``,
``routes/insights.py``, ``portal/delivery.py``) instead of each re-typing
the same action-name literals independently. See ``tests/
test_unresolved_events.py`` for the underlying store method's own
correctness tests (resolution matching, scoping, etc.) -- these tests only
cover that the wrappers pass the right constants through.
"""
from __future__ import annotations

from agentit.portal.pending_actions import (
    FINDING_ESCALATED_ACTION,
    FINDING_ESCALATION_RESOLVED_ACTIONS,
    ROLLBACK_RECOMMENDED_ACTION,
    ROLLBACK_RESOLVED_ACTIONS,
    list_unresolved_escalations,
    list_unresolved_recommendations,
    list_unresolved_rollbacks,
)
from conftest import make_store


async def test_list_unresolved_rollbacks_returns_unresolved_only():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    resolved_id = await store.log_event("slo-tracker", "rollback-recommended", "app-b", "critical", "breach")
    await store.log_event("human", "rollback-dismissed", "app-b", "info", "dismissed", correlation_id=resolved_id)

    unresolved = await list_unresolved_rollbacks(store)

    assert len(unresolved) == 1
    assert unresolved[0]["target_app"] == "app-a"


async def test_list_unresolved_rollbacks_scoped_to_target_app():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event("slo-tracker", "rollback-recommended", "app-b", "critical", "breach")

    unresolved = await list_unresolved_rollbacks(store, target_app="app-a")

    assert len(unresolved) == 1
    assert unresolved[0]["target_app"] == "app-a"


async def test_list_unresolved_escalations_returns_unresolved_only():
    store = await make_store()
    await store.log_event("delivery-verifier", "finding-escalated", "app-a", "critical", "escalated")
    resolved_id = await store.log_event("delivery-verifier", "finding-escalated", "app-b", "critical", "escalated")
    await store.log_event(
        "human", "finding-escalation-acknowledged", "app-b", "info", "ack", correlation_id=resolved_id,
    )

    unresolved = await list_unresolved_escalations(store)

    assert len(unresolved) == 1
    assert unresolved[0]["target_app"] == "app-a"


async def test_list_unresolved_escalations_scoped_to_target_app():
    store = await make_store()
    await store.log_event("delivery-verifier", "finding-escalated", "app-a", "critical", "escalated")
    await store.log_event("delivery-verifier", "finding-escalated", "app-b", "critical", "escalated")

    unresolved = await list_unresolved_escalations(store, target_app="app-b")

    assert len(unresolved) == 1
    assert unresolved[0]["target_app"] == "app-b"


async def test_list_unresolved_recommendations_returns_both_halves_as_a_pair():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event("delivery-verifier", "finding-escalated", "app-a", "critical", "escalated")

    rollbacks, escalations = await list_unresolved_recommendations(store, target_app="app-a")

    assert len(rollbacks) == 1
    assert rollbacks[0]["action"] == "rollback-recommended"
    assert len(escalations) == 1
    assert escalations[0]["action"] == "finding-escalated"


async def test_list_unresolved_recommendations_fleet_wide_when_no_target_app():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event("slo-tracker", "rollback-recommended", "app-b", "critical", "breach")

    rollbacks, escalations = await list_unresolved_recommendations(store)

    assert len(rollbacks) == 2
    assert escalations == []


def test_action_name_constants_match_what_recommendations_py_resolves():
    """``routes/recommendations.py`` re-exports these same four constants
    (rather than defining its own) -- this pins the actual string values so
    a future edit to either module can't silently drift the two apart."""
    assert ROLLBACK_RECOMMENDED_ACTION == "rollback-recommended"
    assert ROLLBACK_RESOLVED_ACTIONS == ["rollback-executed", "rollback-dismissed"]
    assert FINDING_ESCALATED_ACTION == "finding-escalated"
    assert FINDING_ESCALATION_RESOLVED_ACTIONS == ["finding-escalation-acknowledged"]
