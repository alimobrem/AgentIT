"""Human approval gate queue: list, resolve, cancel."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from agentit.audit import audit_log
from agentit.portal.delivery import (
    _CICD_SHARED_NAMESPACE_GATE_TYPE,
    complete_remediations,
    route_and_deliver,
)
from agentit.portal.helpers import get_current_user, get_store

log = logging.getLogger(__name__)

router = APIRouter()


def _gate_redirect_target(gate: dict) -> str:
    """Where a human lands after resolving a gate that doesn't already
    redirect somewhere more specific (approved gates with an assessment_id
    mostly redirect straight to that app's onboard-results -- see below).
    Every gate type is per-app now (the cross-app ``cluster-admin-review``
    gate type / Admin Review page were removed 2026-07-18 -- see delivery.py)
    and resolved from that app's own Assessment Detail Actions tab -- lands
    with ``?tab=actions`` so the NEXT pending gate in that same queue is
    immediately visible instead of dropping the reviewer back on the
    Overview tab (docs/ux-design-requirements.md checklist #12: "redirect to
    the next actionable item, not back to the same page")."""
    assessment_id = gate.get("assessment_id")
    if assessment_id:
        return f"/assessments/{assessment_id}?tab=actions"
    return "/ledger"


@router.get("/gates")
async def gates_page_redirect():
    """The global Gates page is retired (docs/ui-redesign-proposal.md §2/§5)
    -- every gate type now lives on Fleet + Assessment Detail (Admin Review,
    the last standalone gate page, was retired 2026-07-18 along with
    `cluster-admin-review`). Kept as a redirect, not a 404, for any stale
    bookmark/link."""
    return RedirectResponse(url="/ledger", status_code=301)


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

        if gate.get("gate_type") in ("gitops-pr-pending", _CICD_SHARED_NAMESPACE_GATE_TYPE):
            # AutoMode already opened the PR autonomously when it created
            # this gate (automode.py::execute) -- approving it is a merge,
            # never a re-delivery, since these manifests were never meant
            # to be applied directly for this GitOps-registered app at all
            # (see docs/unified-apply-flow.md section (B)). The CI/CD-
            # shared-namespace variant (2026-07-18) is treated identically
            # here -- it's the exact same "merge the PR named in this
            # gate's own summary" action, just under a distinct gate_type
            # so it can coexist with a same-app, same-call cluster-config
            # gate without store.create_gate()'s dedup colliding the two
            # (see delivery.py's _deliver_via_gitops_pr_and_gate()).
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
            # The cluster-config fix this PR carries isn't actually
            # delivered until this merge -- unlike every other delivery
            # mechanism, opening the PR (automode.py::_finish_gitops_pr,
            # route_and_deliver()'s own gate creation) never completes the
            # remediation on its own. This is the one point that confirms
            # the merge actually happened, so it's the one point that can
            # honestly mark the remediation done.
            await complete_remediations(s, assessment_id)
            return RedirectResponse(
                # gitops-pr-pending gates only ever exist for a merged commit
                # against the GitOps infra repo (see the comment above) --
                # pr_url_repo=gitops lets onboard_results.html's flash alert
                # label this PR link instead of showing a bare URL.
                url=f"/assessments/{assessment_id}/onboard-results?pr_url={pr_url}&pr_url_repo=gitops&gate_approved=true",
                status_code=303,
            )

        # cluster-conflict-review (a server-side-apply field-manager
        # conflict, previously surfaced through a force=True re-apply) and
        # cluster-admin-review (CI/CD manifests destined for a shared
        # operator namespace, previously an elevated-RBAC direct apply via
        # apply_with_verification(..., allow_operator_namespaces=True)) have
        # both been removed along with Direct Apply as a concept entirely --
        # apply_manifests_to_cluster()/kube.apply_yaml() are no longer called
        # anywhere in this app for delivery purposes (see
        # route_and_deliver()/resolve_cluster_config_mechanism()); CI/CD
        # manifests for a shared namespace now resolve to
        # MECHANISM_INFRA_REPO_COMMIT exactly like cluster-config (see
        # delivery.py's CATEGORY_CICD_SHARED_NAMESPACE handling). Neither
        # gate type can be created by any code path in this app anymore; if
        # one somehow still exists (e.g. stale data from before this
        # directive), it now falls through to the generic delivery branch
        # below, same as any other unrecognized gate type -- re-classifying
        # this assessment's files fresh and routing any still-cicd ones
        # through the same GitOps PR path a brand-new delivery would use.

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
    target = _gate_redirect_target(gate) if gate else "/ledger"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}success=Gate+dismissed", status_code=303)


@router.get("/api/gates")
async def api_gates(status: str = "pending"):
    s = await get_store()
    return JSONResponse(await s.list_gates(status=status))
