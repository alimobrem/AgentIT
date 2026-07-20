"""Real, direct actions for the two recommendation types that don't fit the
"there's a real GitHub PR to merge" shape the rest of the (now-removed)
gates system collapsed into:

- ``rollback-review`` -- an SLO breach recommending a rollback. There's no
  PR to track here; this is a genuine human DECISION (roll back, or don't).
  Resolved by a real action: ``execute_rollback`` actually performs the
  rollback (``remediation_loop.rollback_action`` -> ``kube.rollout_undo``,
  the same real mechanism the direct-apply verification tail already uses),
  or ``dismiss_rollback`` records that a human looked at it and chose not
  to.
- ``finding-unresolved-escalation`` -- a finding that's exhausted its
  bounded auto-retry budget (delivery.py's Phase 4). Acknowledging it is a
  pure "a human has seen this" signal, never a re-delivery.

Neither is tracked by a separately-maintained ``gates`` row: both are a
plain ``events`` row (``slo-tracker``/``delivery.py`` already logs the
recommendation itself) with no later correlated resolving event -- see
``store.list_unresolved_events()``.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from agentit.audit import audit_log
from agentit.portal.helpers import get_current_user, get_store

router = APIRouter()

ROLLBACK_RECOMMENDED_ACTION = "rollback-recommended"
ROLLBACK_RESOLVED_ACTIONS = ["rollback-executed", "rollback-dismissed"]

FINDING_ESCALATED_ACTION = "finding-escalated"
FINDING_ESCALATION_RESOLVED_ACTIONS = ["finding-escalation-acknowledged"]


def _redirect_target(assessment_id: str | None) -> str:
    """Same "land on the Ledger tab, not the Overview tab" convention the
    retired ``routes/gates.py::resolve_gate()`` used (Actions was merged
    into Ledger 2026-07-19, see ``routes/pr_actions.py``'s own
    ``_redirect_target()``) -- the next actionable item in the same queue
    is immediately visible."""
    if assessment_id:
        return f"/assessments/{assessment_id}?tab=ledger"
    return "/ledger"


async def _require_recommendation(store: object, event_id: str, action: str) -> dict:
    event = await store.get_event(event_id)
    if event is None or event.get("action") != action:
        raise HTTPException(404, "Recommendation not found")
    return event


@router.post("/rollback/{event_id}/execute", response_model=None)
async def execute_rollback(request: Request, event_id: str):
    form = await request.form()
    assessment_id = str(form.get("assessment_id") or "") or None
    actor = get_current_user(request)
    s = await get_store()

    event = await _require_recommendation(s, event_id, ROLLBACK_RECOMMENDED_ACTION)
    app_name = event["target_app"]

    apply_result = await s.get_apply_results(assessment_id) if assessment_id else None
    namespace = (apply_result or {}).get("namespace") or app_name

    from agentit.remediation_loop import rollback_action

    result = await rollback_action(app_name, namespace)
    succeeded = result["outcome"] == "rolled_back"

    audit_log(
        actor=actor, action="rollback-execute", resource=f"app:{app_name}",
        outcome=result["outcome"], details={"event_id": event_id, "namespace": namespace},
    )
    await s.log_event(
        "human", "rollback-executed", app_name, "info" if succeeded else "warning",
        f"Rollback {'executed' if succeeded else 'failed'} for {app_name}: "
        f"{result.get('details') or result.get('error', '')}",
        correlation_id=event_id,
    )

    target = _redirect_target(assessment_id)
    sep = "&" if "?" in target else "?"
    if not succeeded:
        return RedirectResponse(
            url=f"{target}{sep}error={quote('Rollback failed: ' + str(result.get('error', ''))[:150])}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"{target}{sep}success={quote(f'Rollback executed for {app_name}')}", status_code=303,
    )


@router.post("/rollback/{event_id}/dismiss", response_model=None)
async def dismiss_rollback(request: Request, event_id: str):
    form = await request.form()
    assessment_id = str(form.get("assessment_id") or "") or None
    actor = get_current_user(request)
    s = await get_store()

    event = await _require_recommendation(s, event_id, ROLLBACK_RECOMMENDED_ACTION)
    app_name = event["target_app"]

    audit_log(actor=actor, action="rollback-dismiss", resource=f"app:{app_name}", outcome="dismissed")
    await s.log_event(
        "human", "rollback-dismissed", app_name, "info",
        f"Rollback recommendation dismissed for {app_name} -- no rollback performed.",
        correlation_id=event_id,
    )

    target = _redirect_target(assessment_id)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}success=Rollback+recommendation+dismissed", status_code=303)


@router.post("/findings/{event_id}/acknowledge", response_model=None)
async def acknowledge_finding_escalation(request: Request, event_id: str):
    form = await request.form()
    assessment_id = str(form.get("assessment_id") or "") or None
    actor = get_current_user(request)
    s = await get_store()

    event = await _require_recommendation(s, event_id, FINDING_ESCALATED_ACTION)
    app_name = event["target_app"]

    audit_log(actor=actor, action="finding-escalation-acknowledge", resource=f"app:{app_name}", outcome="acknowledged")
    await s.log_event(
        "human", "finding-escalation-acknowledged", app_name, "info",
        f"Escalated finding acknowledged for {app_name} -- no automatic re-delivery was triggered by this acknowledgment.",
        correlation_id=event_id,
    )

    target = _redirect_target(assessment_id)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}success=Escalation+acknowledged.", status_code=303)
