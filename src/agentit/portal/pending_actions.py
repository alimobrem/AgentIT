"""Single, shared definition of the "rollback recommendation + escalation"
unresolved-event pair -- the plain-``events`` mechanism that replaced the
retired ``gates`` table for the two recommendation types that don't
resolve to a real GitHub PR (see ``routes/recommendations.py``'s module
docstring): ``rollback-review`` (an SLO-breach rollback decision) and
``finding-unresolved-escalation`` (Phase 4's bounded-auto-retry stop
condition).

Before this module existed, ``store.list_unresolved_events(action,
resolved_actions, ...)`` was called with these same four action-name
literals re-typed independently in ``routes/assessments.py``,
``routes/fleet.py`` (two call sites), ``routes/insights.py``, and
``portal/delivery.py`` -- each copy free to drift from the others (see
``ROLLBACK_RECOMMENDED_ACTION``'s docstring note below for one case that
already had). These wrappers are pure pass-throughs to
``AssessmentStore.list_unresolved_events()`` (unchanged, still owned by
``store.py``) -- they exist only to give every caller one place to get the
action-name pairing right, not a new data path.

Deliberately NOT what ``helpers.get_nav_pending_action_counts()`` (the nav
badge) uses: that count is PR-status-derived only (``pr_tracking.
count_fleet_prs_waiting_for_approval()``), by design -- see that function's
own module-level comment in ``helpers.py``.
"""
from __future__ import annotations

ROLLBACK_RECOMMENDED_ACTION = "rollback-recommended"
ROLLBACK_RESOLVED_ACTIONS = ["rollback-executed", "rollback-dismissed"]

FINDING_ESCALATED_ACTION = "finding-escalated"
FINDING_ESCALATION_RESOLVED_ACTIONS = ["finding-escalation-acknowledged"]


async def list_unresolved_rollbacks(store: object, target_app: str | None = None) -> list[dict]:
    """Every unresolved ``rollback-recommended`` event, fleet-wide (default)
    or scoped to one app via ``target_app``."""
    return await store.list_unresolved_events(
        ROLLBACK_RECOMMENDED_ACTION, ROLLBACK_RESOLVED_ACTIONS, target_app=target_app,
    )


async def list_unresolved_escalations(store: object, target_app: str | None = None) -> list[dict]:
    """Every unresolved ``finding-escalated`` event, fleet-wide (default) or
    scoped to one app via ``target_app``."""
    return await store.list_unresolved_events(
        FINDING_ESCALATED_ACTION, FINDING_ESCALATION_RESOLVED_ACTIONS, target_app=target_app,
    )


async def list_unresolved_recommendations(
    store: object, target_app: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Both halves of the pair together, as ``(rollbacks, escalations)`` --
    the shape every current call site that needs both actually wants
    (``routes/assessments.py``, ``routes/fleet.py::_attach_pending_actions``,
    ``routes/insights.py``). Two real queries, same as before this module
    existed -- this only removes the duplicated action-name literals, not a
    query."""
    rollbacks = await list_unresolved_rollbacks(store, target_app=target_app)
    escalations = await list_unresolved_escalations(store, target_app=target_app)
    return rollbacks, escalations
