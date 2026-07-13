"""Human approval gate queue: list, resolve, cancel."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.audit import audit_log
from agentit.portal.cluster_apply import apply_manifests_to_cluster
from agentit.portal.helpers import get_current_user, get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/gates", response_class=HTMLResponse)
async def gates_page(request: Request):
    """Show pending approval gates. Auto-expires gates older than 24h."""
    s = await get_store()
    expired_count = await s.expire_stale_gates(hours=24)
    if expired_count:
        await s.log_event("portal", "gates-expired", None, "info",
                           f"Auto-expired {expired_count} stale gate(s)")

    all_gates = await s.list_all_gates()
    pending = [g for g in all_gates if g["status"] == "pending"]
    stale = await s.get_stale_gates(hours=4)
    stale_ids = {g["id"] for g in stale}
    for g in pending:
        g["stale"] = g["id"] in stale_ids
    resolved = [g for g in all_gates if g["status"] in ("approved", "rejected", "expired")]
    resolved.sort(key=lambda g: g.get("resolved_at") or g.get("created_at", ""), reverse=True)
    return get_templates().TemplateResponse(request, "gates.html", {
        "pending": pending, "resolved": resolved[:20],
        "stale_count": len(stale), "expired_count": expired_count,
    })


@router.post("/gates/{gate_id}/resolve", response_model=None)
async def resolve_gate(request: Request, gate_id: str):
    form = await request.form()
    status = form.get("status")
    if status not in ("approved", "rejected", "dismissed"):
        raise HTTPException(400, "Invalid status: must be approved, rejected, or dismissed")
    resolved_by = form.get("resolved_by") or get_current_user(request)
    s = await get_store()

    gates = await s.list_gates(status="pending")
    gate = next((g for g in gates if g["id"] == gate_id), None)
    if gate is None:
        raise HTTPException(404, "Gate not found")

    audit_log(actor=str(resolved_by), action=f"gate-{status}", resource=f"gate:{gate_id}",
              details={"gate_type": gate.get("gate_type"), "assessment_id": gate.get("assessment_id")})

    if status == "approved" and gate.get("assessment_id"):
        assessment_id = gate["assessment_id"]

        if gate.get("gate_type") == "rollback-review":
            await s.resolve_gate(gate_id, status, resolved_by)
            await s.log_event(
                "gate-resolver", "rollback-approved",
                gate.get("target_app"), "warning",
                f"Rollback approved for assessment {assessment_id} — manual intervention required",
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}?success=Rollback+approved.+Review+the+deployment+and+roll+back+manually+or+via+Argo+Rollouts.",
                status_code=303,
            )

        files = await s.get_onboarding(assessment_id)
        report = await s.get(assessment_id)
        if files and report:
            namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
            try:
                results = await asyncio.to_thread(
                    apply_manifests_to_cluster, files, namespace, False,
                )
            except Exception:
                log.exception("Manifest apply failed for gate %s (assessment %s)", gate_id, assessment_id)
                return RedirectResponse(
                    url=f"/assessments/{assessment_id}/onboard-results?error={quote('Manifest apply failed — gate remains pending')}",
                    status_code=303,
                )
            await s.resolve_gate(gate_id, status, resolved_by)
            await s.save_apply_results(assessment_id, results, namespace, False)

            from agentit.skill_engine import record_skill_outcomes
            await record_skill_outcomes(
                s, report.repo_name, files, set(results["applied"]), "approved",
                f"gate {gate_id} approved by {resolved_by}",
            )

            applied = len(results["applied"])
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?applied={applied}&gate_approved=true",
                status_code=303,
            )

    await s.resolve_gate(gate_id, status, resolved_by)

    if status == "rejected":
        reject_reason = str(form.get("reason", ""))
        await s.record_feedback(
            app_name=gate.get("target_app", ""),
            agent_name=gate.get("agent_name", "gate"),
            finding_category=gate.get("gate_type", ""),
            action="rejected",
            human_reason=reject_reason,
        )

        # Also record a per-skill outcome for every skill-generated file this
        # gate covered -- the agent_feedback write above is generic
        # (agent_name/gate_type), not attributed to the specific skill(s)
        # that produced the rejected manifests, so skill_effectiveness never
        # saw a negative signal from a gate rejection until now.
        reject_assessment_id = gate.get("assessment_id")
        if reject_assessment_id:
            reject_files = await s.get_onboarding(reject_assessment_id)
            reject_report = await s.get(reject_assessment_id)
            if reject_files and reject_report:
                from agentit.skill_engine import record_skill_outcomes
                await record_skill_outcomes(
                    s, reject_report.repo_name, reject_files, None, "rejected",
                    reject_reason,
                )

    return RedirectResponse(url="/gates", status_code=303)


@router.post("/gates/{gate_id}/cancel", response_model=None)
async def cancel_gate(request: Request, gate_id: str):
    s = await get_store()
    await s.resolve_gate(gate_id, "cancelled", get_current_user(request))
    return RedirectResponse(url="/gates?success=Gate+dismissed", status_code=303)


@router.get("/api/gates")
async def api_gates(status: str = "pending"):
    s = await get_store()
    return JSONResponse(await s.list_gates(status=status))
