"""Human approval gate queue: list, resolve, cancel."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentit.audit import audit_log
from agentit.portal.cluster_apply import apply_with_verification
from agentit.portal.delivery import (
    ADMIN_REVIEW_GATE_TYPE,
    CATEGORY_CICD_SHARED_NAMESPACE,
    classify_file,
    gate_delivery_confirmation,
    route_and_deliver,
)
from agentit.portal.helpers import get_current_user, get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


def _gate_redirect_target(gate: dict) -> str:
    """Where a human lands after resolving a gate that doesn't already
    redirect somewhere more specific (approved gates with an assessment_id
    mostly redirect straight to that app's onboard-results -- see below).
    Cluster-admin-review gates are cross-app and resolved from Admin
    Review; every other gate type is per-app and resolved from that app's
    own Assessment Detail Actions tab -- lands with ``?tab=actions`` so
    the NEXT pending gate in that same queue is immediately visible
    instead of dropping the reviewer back on the Overview tab
    (docs/ux-design-requirements.md checklist #12: "redirect to the next
    actionable item, not back to the same page")."""
    if gate.get("gate_type") == ADMIN_REVIEW_GATE_TYPE:
        return "/admin-review"
    assessment_id = gate.get("assessment_id")
    if assessment_id:
        return f"/assessments/{assessment_id}?tab=actions"
    return "/admin-review"


async def _admin_review_redirect_after_resolve(store: object, applied: int | None = None) -> str | None:
    """After resolving a cluster-admin-review gate: if another one is
    still pending, jump straight back to the Admin Review queue (the next
    actionable item, matching checklist #12) instead of onboard-results --
    a reviewer working through several pending elevated-review gates in a
    row never has to manually navigate back. Returns ``None`` once the
    queue is genuinely empty, so the caller falls through to its normal
    onboard-results redirect (showing the delivery outcome instead)."""
    remaining = await store.list_gates(status="pending")
    if any(g.get("gate_type") == ADMIN_REVIEW_GATE_TYPE for g in remaining):
        params = "gate_approved=true"
        if applied is not None:
            params += f"&applied={applied}"
        return f"/admin-review?{params}"
    return None


@router.get("/gates")
async def gates_page_redirect():
    """The global Gates page is retired (docs/ui-redesign-proposal.md §2/§5)
    -- the 7 app-owner gate types now live on Fleet + Assessment Detail;
    only `cluster-admin-review` still gets a standalone page. Kept as a
    redirect, not a 404, for any stale bookmark/link."""
    return RedirectResponse(url="/admin-review", status_code=301)


@router.get("/admin-review", response_class=HTMLResponse)
async def admin_review_page(request: Request):
    """Show pending cluster-admin-review gates only -- the one gate type
    that's genuinely cross-app, for a genuinely different audience than an
    app owner. Auto-expires gates older than 24h, same as the retired
    global Gates page did."""
    s = await get_store()
    expired_count = await s.expire_stale_gates(hours=24)
    if expired_count:
        await s.log_event("portal", "gates-expired", None, "info",
                           f"Auto-expired {expired_count} stale gate(s)")

    all_gates = await s.list_all_gates()
    pending = [g for g in all_gates if g["status"] == "pending" and g["gate_type"] == ADMIN_REVIEW_GATE_TYPE]
    resolved = [g for g in all_gates if g["status"] in ("approved", "rejected", "expired") and g["gate_type"] == ADMIN_REVIEW_GATE_TYPE]
    stale = await s.get_stale_gates(hours=4)
    stale_ids = {g["id"] for g in stale}
    for g in pending:
        g["stale"] = g["id"] in stale_ids
        g["delivery_confirmation"] = await gate_delivery_confirmation(s, g)
    resolved.sort(key=lambda g: g.get("resolved_at") or g.get("created_at", ""), reverse=True)

    # Real, specific empty-state copy (docs/ux-design-requirements.md
    # checklist #10) instead of a bare "all clear" -- how many of THIS
    # gate type were actually resolved in the last 24h, sourced from the
    # same `resolved` list already computed above, never fabricated.
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recently_resolved_count = sum(
        1 for g in resolved if (g.get("resolved_at") or g.get("created_at") or "") >= cutoff
    )

    return get_templates().TemplateResponse(request, "admin_review.html", {
        "pending": pending, "resolved": resolved[:20],
        "stale_count": sum(1 for g in pending if g["id"] in stale_ids),
        "expired_count": expired_count,
        "recently_resolved_count": recently_resolved_count,
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

    # Atomically claim the gate BEFORE performing any of the side effects
    # below (cluster apply / PR merge / GitOps commit) -- `resolve_gate()`'s
    # `UPDATE ... WHERE status = 'pending'` only succeeds for the first of
    # any near-simultaneous resolve requests for this gate_id; a second one
    # loses the claim and bails out here instead of racing the first
    # through the same side effect. If a claimed side effect below then
    # fails, that branch calls `s.reopen_gate(gate_id, status)` to put the
    # gate back to `pending` rather than leaving it falsely marked resolved.
    claimed = await s.resolve_gate(gate_id, status, resolved_by)
    if not claimed:
        target = _gate_redirect_target(gate)
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{sep}error={quote('This gate was already resolved by another request')}",
            status_code=303,
        )

    audit_log(actor=str(resolved_by), action=f"gate-{status}", resource=f"gate:{gate_id}",
              details={"gate_type": gate.get("gate_type"), "assessment_id": gate.get("assessment_id")})

    if status == "approved" and gate.get("assessment_id"):
        assessment_id = gate["assessment_id"]

        if gate.get("gate_type") == "rollback-review":
            await s.log_event(
                "gate-resolver", "rollback-approved",
                gate.get("app_name"), "warning",
                f"Rollback approved for assessment {assessment_id} — manual intervention required",
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}?tab=actions&success=Rollback+approved.+Review+the+deployment+and+roll+back+manually+or+via+Argo+Rollouts.",
                status_code=303,
            )

        if gate.get("gate_type") == "finding-unresolved-escalation":
            # A finding that's failed FINDING_ESCALATION_THRESHOLD confirmed
            # automated fix attempts (delivery.py's escalate_unresolved_
            # finding()) -- approving this gate is a pure acknowledgment
            # that a human has looked at it, never a re-delivery: falling
            # through to the generic branch below would re-deliver this
            # app's ENTIRE onboarding batch, not just retry the one
            # escalated finding, which is not what approving this gate
            # should do.
            await s.log_event(
                "gate-resolver", "finding-escalation-acknowledged",
                gate.get("app_name"), "info",
                f"Escalated finding acknowledged for assessment {assessment_id} — "
                "no automatic re-delivery was triggered by this approval.",
            )
            return RedirectResponse(
                url=f"/assessments/{assessment_id}?tab=actions&success=Escalation+acknowledged.",
                status_code=303,
            )

        if gate.get("gate_type") == "gitops-pr-pending":
            # AutoMode already opened the PR autonomously when it created
            # this gate (automode.py::execute) -- approving it is a merge,
            # never a re-delivery, since these manifests were never meant
            # to be applied directly for this GitOps-registered app at all
            # (see docs/unified-apply-flow.md section (B)).
            from agentit.portal.github_pr import merge_pr

            pr_url = ""
            summary = gate.get("summary", "")
            for token in summary.split():
                if token.startswith("http") and "/pull/" in token:
                    pr_url = token.rstrip(".,")
                    break
            if not pr_url:
                await s.reopen_gate(gate_id, status)
                return RedirectResponse(
                    url=f"/assessments/{assessment_id}/onboard-results?error={quote('No PR URL found on this gate — cannot merge')}",
                    status_code=303,
                )
            merge_result = await asyncio.to_thread(merge_pr, pr_url)
            if "error" in merge_result:
                await s.reopen_gate(gate_id, status)
                log.warning("PR merge failed for gate %s: %s", gate_id, merge_result["error"])
                return RedirectResponse(
                    url=f"/assessments/{assessment_id}/onboard-results?error={quote('PR merge failed: ' + merge_result['error'][:150])}",
                    status_code=303,
                )
            await s.log_event(
                "gate-resolver", "gitops-pr-merged", gate.get("app_name"), "info",
                f"Merged GitOps PR {pr_url} for assessment {assessment_id}",
            )
            return RedirectResponse(
                # gitops-pr-pending gates only ever exist for a merged commit
                # against the GitOps infra repo (see the comment above) --
                # pr_url_repo=gitops lets onboard_results.html's flash alert
                # label this PR link instead of showing a bare URL.
                url=f"/assessments/{assessment_id}/onboard-results?pr_url={pr_url}&pr_url_repo=gitops&gate_approved=true",
                status_code=303,
            )

        if gate.get("gate_type") == "cluster-admin-review":
            # A human holding elevated RBAC has explicitly approved applying
            # directly into a shared operator namespace -- the one case
            # where the delivery router's own "never a direct apply into a
            # shared operator namespace" rule is deliberately bypassed, by
            # explicit human approval, not a code path that decides this on
            # its own (see docs/unified-apply-flow.md's "CI/CD needs its
            # own lane" section).
            files = await s.get_onboarding(assessment_id)
            report = await s.get(assessment_id)
            if files and report:
                cicd_files = [f for f in files if classify_file(f) == CATEGORY_CICD_SHARED_NAMESPACE]
                namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
                try:
                    results = await apply_with_verification(
                        cicd_files, namespace, False,
                        store=s, app_name=report.repo_name,
                        skill_outcome_reason=f"cluster-admin-review gate {gate_id} approved by {resolved_by}",
                        actor=str(resolved_by), action="cluster-admin-apply",
                        resource=f"assessment:{assessment_id}",
                        allow_operator_namespaces=True,
                    )
                except Exception as exc:
                    await s.reopen_gate(gate_id, status)
                    log.exception("Elevated apply failed for gate %s (assessment %s)", gate_id, assessment_id)
                    return RedirectResponse(
                        url=(
                            f"/assessments/{assessment_id}/onboard-results?error="
                            f"{quote(f'Elevated apply failed: {str(exc)[:150]}. The gate remains pending — fix the issue and re-approve, or Reject with a reason.')}"
                        ),
                        status_code=303,
                    )
                applied = len(results["applied"])
                # Redirect to the NEXT pending cluster-admin-review gate (if
                # any) rather than always landing on this app's own
                # onboard-results -- a reviewer working through several
                # elevated-review gates in a row shouldn't have to
                # manually navigate back each time (docs/ux-design-
                # requirements.md checklist #12).
                next_url = await _admin_review_redirect_after_resolve(s, applied=applied)
                return RedirectResponse(
                    url=next_url or f"/assessments/{assessment_id}/onboard-results?applied={applied}&gate_approved=true",
                    status_code=303,
                )

        # cluster-conflict-review (a server-side-apply field-manager
        # conflict, surfaced through apply_with_verification()'s force=True
        # re-apply) has been removed along with Direct Apply as a concept
        # entirely -- apply_manifests_to_cluster()/kube.apply_yaml() are
        # never called for the cluster-config category anymore, so this
        # conflict can no longer genuinely occur (see
        # route_and_deliver()/resolve_cluster_config_mechanism()). This gate
        # type can no longer be created by any code path in this app; if one
        # somehow still exists (e.g. stale data from before this directive),
        # it now falls through to the generic delivery branch below, same as
        # any other unrecognized gate type.

        files = await s.get_onboarding(assessment_id)
        report = await s.get(assessment_id)
        if files and report:
            namespace = report.repo_name.lower().replace("_", "-").replace(".", "-")
            try:
                delivery = await route_and_deliver(
                    files, app_name=report.repo_name, namespace=namespace,
                    report=report, store=s, assessment_id=assessment_id,
                    actor=str(resolved_by), dry_run=False,
                )
            except Exception as exc:
                await s.reopen_gate(gate_id, status)
                log.exception("Delivery failed for gate %s (assessment %s)", gate_id, assessment_id)
                return RedirectResponse(
                    url=(
                        f"/assessments/{assessment_id}/onboard-results?error="
                        f"{quote(f'Delivery failed: {str(exc)[:150]}. The gate remains pending — fix the issue and re-approve, or Reject with a reason.')}"
                    ),
                    status_code=303,
                )

            cluster_outcome = delivery["outcomes"].get("cluster_config", {})
            applied = len(cluster_outcome.get("applied", [])) if isinstance(cluster_outcome, dict) else 0
            return RedirectResponse(
                url=f"/assessments/{assessment_id}/onboard-results?applied={applied}&gate_approved=true",
                status_code=303,
            )

    if status == "rejected":
        reject_reason = str(form.get("reason", ""))
        # Bug fix: `gate` comes from `list_gates()` (store.py), whose join
        # aliases the app name as `app_name`, not `target_app` -- reading
        # `target_app` here (a column that exists on `events`, not on this
        # gate dict) always evaluated to "", so every gate-rejection
        # feedback row was silently recorded against no app at all,
        # invisible to any per-app lookup (e.g. pr_tracking.py's rejection-
        # reason correlation for PR History).
        await s.record_feedback(
            app_name=gate.get("app_name", ""),
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

    return RedirectResponse(url=_gate_redirect_target(gate), status_code=303)


@router.post("/gates/{gate_id}/cancel", response_model=None)
async def cancel_gate(request: Request, gate_id: str):
    s = await get_store()
    gates = await s.list_gates(status="pending")
    gate = next((g for g in gates if g["id"] == gate_id), None)
    await s.resolve_gate(gate_id, "cancelled", get_current_user(request))
    target = _gate_redirect_target(gate) if gate else "/admin-review"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}success=Gate+dismissed", status_code=303)


@router.get("/api/gates")
async def api_gates(status: str = "pending"):
    s = await get_store()
    return JSONResponse(await s.list_gates(status=status))
