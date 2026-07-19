"""store.list_unresolved_events() -- the plain-events "still needs a human
decision" mechanism that replaced the gates table for non-PR
recommendations (rollback-review, finding-unresolved-escalation)."""
from __future__ import annotations

from conftest import make_store


async def test_event_with_no_resolution_is_unresolved():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    unresolved = await store.list_unresolved_events("rollback-recommended", ["rollback-executed", "rollback-dismissed"])
    assert len(unresolved) == 1
    assert unresolved[0]["target_app"] == "app-a"


async def test_resolved_event_is_excluded():
    store = await make_store()
    event_id = await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event(
        "human", "rollback-dismissed", "app-a", "info", "dismissed", correlation_id=event_id,
    )
    unresolved = await store.list_unresolved_events("rollback-recommended", ["rollback-executed", "rollback-dismissed"])
    assert unresolved == []


async def test_resolution_with_unrelated_action_does_not_resolve():
    store = await make_store()
    event_id = await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event("human", "something-else", "app-a", "info", "noise", correlation_id=event_id)
    unresolved = await store.list_unresolved_events("rollback-recommended", ["rollback-executed", "rollback-dismissed"])
    assert len(unresolved) == 1


async def test_scoped_to_target_app():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event("slo-tracker", "rollback-recommended", "app-b", "critical", "breach")
    unresolved = await store.list_unresolved_events(
        "rollback-recommended", ["rollback-executed", "rollback-dismissed"], target_app="app-a",
    )
    assert len(unresolved) == 1
    assert unresolved[0]["target_app"] == "app-a"


async def test_fleet_wide_when_no_target_app_given():
    store = await make_store()
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach")
    await store.log_event("slo-tracker", "rollback-recommended", "app-b", "critical", "breach")
    unresolved = await store.list_unresolved_events("rollback-recommended", ["rollback-executed", "rollback-dismissed"])
    assert len(unresolved) == 2


async def test_a_resolved_event_for_one_of_two_recommendations_leaves_the_other_unresolved():
    store = await make_store()
    first_id = await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach 1")
    await store.log_event("slo-tracker", "rollback-recommended", "app-a", "critical", "breach 2")
    await store.log_event("human", "rollback-dismissed", "app-a", "info", "dismissed", correlation_id=first_id)
    unresolved = await store.list_unresolved_events("rollback-recommended", ["rollback-executed", "rollback-dismissed"])
    assert len(unresolved) == 1
    assert unresolved[0]["summary"] == "breach 2"
