"""Webhook endpoints: /api/webhook/*"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from agentit.portal.helpers import (
    clone_assess_cleanup, get_llm_client, get_store, publish_event, run_onboarding, with_timeout,
)

log = logging.getLogger(__name__)


def _get_delivery_id(request: Request, body: dict) -> str:
    """Get a unique delivery ID from GitHub header or body hash."""
    gh_delivery = request.headers.get("X-GitHub-Delivery")
    if gh_delivery:
        return gh_delivery
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]


def _verify_github_signature(request: Request, body_bytes: bytes) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature. Skips if secret not set (dev mode)."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        return True
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig_header[7:], expected)


INTERNAL_TOKEN_HEADER = "X-Internal-Webhook-Token"


def verify_internal_token(request: Request) -> None:
    """Shared-secret auth for the in-cluster-only webhook routes below.

    These are called by Argo Events Sensors (chart/templates/argo-events/
    sensor-*.yaml) hitting the Service directly (`http://agentit.agentit.svc:
    8080/...`), never by a browser -- so the oauth-proxy sidecar (auth.enabled,
    app.py) doesn't protect them regardless of whether it's on. The Sensors'
    HTTP triggers attach the same secret as a `secureHeaders` entry sourced
    from the `agentit-internal-webhook-token` Secret at trigger-fire time.

    Skips the check (fails open) if AGENTIT_INTERNAL_WEBHOOK_TOKEN isn't set
    in this process's env -- matching the existing _verify_github_signature
    convention above -- so local dev/tests that never configure this secret
    keep working. In a real deployment the Secret is always templated (see
    chart/templates/internal-webhook-token-secret.yaml), so that fail-open
    path should never actually be exercised in production.
    """
    secret = os.environ.get("AGENTIT_INTERNAL_WEBHOOK_TOKEN")
    if not secret:
        return
    token = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(401, "Missing or invalid internal webhook token")


router = APIRouter()


@router.post("/api/webhook/assess", dependencies=[Depends(verify_internal_token)])
async def webhook_assess(request: Request):
    """Trigger an assessment via webhook. Accepts JSON body: {repo_url, criticality}"""
    body = await request.json()

    delivery_id = _get_delivery_id(request, body)
    s = await get_store()
    if await s.webhook_already_processed(delivery_id):
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id})

    repo_url = body.get("repo_url")
    if not repo_url:
        raise HTTPException(400, "repo_url required")
    criticality = body.get("criticality", "medium")
    check_results: list[dict] = []
    try:
        report = await with_timeout(
            asyncio.to_thread(clone_assess_cleanup, repo_url, criticality, check_results_out=check_results)
        )
    except Exception:
        from agentit.portal.metrics import assessments_total as _at
        _at.labels(criticality=criticality, status="error").inc()
        raise
    from agentit.portal.metrics import assessments_total as _at
    _at.labels(criticality=criticality, status="success").inc()
    assessment_id = await s.save(report)
    await s.save_check_results(assessment_id, check_results)
    from agentit.events import TOPIC_ASSESSMENTS
    publish_event("assessment-complete", report.repo_name,
                  f"Assessment complete: {report.overall_score:.0f}/100",
                  {"assessment_id": assessment_id, "score": report.overall_score},
                  correlation_id=assessment_id,
                  extra_topic=TOPIC_ASSESSMENTS)
    await s.mark_webhook_processed(delivery_id)
    return JSONResponse({"assessment_id": assessment_id, "overall_score": report.overall_score})


@router.post("/api/webhook/github-push")
async def webhook_github_push(request: Request):
    """Handle GitHub push webhooks -- triggers re-assessment for managed repos."""
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        return JSONResponse({"status": "pong"})
    if event_type != "push":
        return JSONResponse({"status": "ignored", "reason": f"event type '{event_type}' not handled"})

    body_bytes = await request.body()
    if not _verify_github_signature(request, body_bytes):
        raise HTTPException(403, "Invalid webhook signature")

    body = json.loads(body_bytes)

    delivery_id = _get_delivery_id(request, body)
    s_dedup = await get_store()
    if await s_dedup.webhook_already_processed(delivery_id):
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id})

    ref = body.get("ref", "")
    repo_url = body.get("repository", {}).get("html_url", "")
    default_branch = body.get("repository", {}).get("default_branch", "main")

    if ref != f"refs/heads/{default_branch}":
        return JSONResponse({"status": "ignored", "reason": f"push to {ref}, not {default_branch}"})

    if not repo_url:
        raise HTTPException(400, "No repository URL in push payload")

    s = s_dedup
    fleet = await s.get_fleet_data()
    managed = next(
        (app for app in fleet if app["repo_url"].rstrip("/").lower() == repo_url.rstrip("/").lower()),
        None,
    )
    if managed is None:
        return JSONResponse({"status": "ignored", "reason": f"{repo_url} not in fleet"})

    commit_sha = body.get("after", "")[:12]
    pusher = body.get("pusher", {}).get("name", "unknown")
    log.info("GitHub push on managed repo %s by %s (commit %s) -- triggering re-assessment",
             managed["repo_name"], pusher, commit_sha)

    await s.log_event("github-webhook", "push-received", managed["repo_name"],
                       "info", f"Push by {pusher} (commit {commit_sha}) -- re-assessing")

    criticality = managed.get("criticality", "medium")
    check_results: list[dict] = []
    try:
        report = await with_timeout(
            asyncio.to_thread(clone_assess_cleanup, repo_url, criticality, check_results_out=check_results)
        )
        assessment_id = await s.save(report)
        await s.save_check_results(assessment_id, check_results)
        await s.log_event("github-webhook", "reassessment-complete", managed["repo_name"],
                           "info", f"Re-assessment complete: {report.overall_score:.0f}/100 (was {managed.get('latest_score', '?')})")
        from agentit.events import TOPIC_ASSESSMENTS as _TOPIC_ASSESSMENTS
        publish_event("assessment-complete", managed["repo_name"],
                      f"Re-assessment: {report.overall_score:.0f}/100",
                      {"assessment_id": assessment_id, "score": report.overall_score},
                      correlation_id=assessment_id,
                      extra_topic=_TOPIC_ASSESSMENTS)

        # --- Change impact analysis and targeted re-hardening ---
        commits = body.get("commits", [])
        changed_files: list[str] = []
        added_files: list[str] = []
        for c in commits:
            changed_files.extend(c.get("modified", []))
            changed_files.extend(c.get("added", []))
            added_files.extend(c.get("added", []))
        changed_files = list(set(changed_files))

        from agentit.change_analyzer import analyze_changes
        impact = analyze_changes(changed_files, added_files)

        # Diff against previous assessment
        prev_history = await s.list_history(repo_url)
        if len(prev_history) >= 2:
            prev_report = await s.get(prev_history[-2]["id"])
            if prev_report:
                from agentit.assessment_diff import diff_assessments
                diff = diff_assessments(prev_report, report)
                await s.log_event("reassessment", "score-diff", managed["repo_name"],
                                   "warning" if diff.degraded else "info",
                                   diff.summary())

                # Auto-fix new findings that are auto-fixable. RemediationDispatcher
                # is now genuinely async -- await it directly, no more
                # .raw/to_thread bridge needed for this call path.
                if diff.auto_fixable and (await s.get_setting("auto_mode")) == "true":
                    from agentit.remediation.dispatcher import RemediationDispatcher
                    dispatcher = RemediationDispatcher(s)
                    for finding in diff.auto_fixable:
                        if await s.get_rejection_count(managed["repo_name"], finding.category) >= 3:
                            await s.log_event("learning", "skipped-rejected", managed["repo_name"],
                                               "info", f"Skipping {finding.category} -- rejected 3+ times")
                            continue
                        await dispatcher.dispatch(assessment_id, finding.category, managed["repo_name"])

        await s.log_event("change-analysis", "impact-analyzed", managed["repo_name"],
                           "info", impact.summary())

        await s_dedup.mark_webhook_processed(delivery_id)
        return JSONResponse({
            "status": "assessed",
            "repo": managed["repo_name"],
            "assessment_id": assessment_id,
            "score": report.overall_score,
            "previous_score": managed.get("latest_score"),
            "change_impact": {
                "files_changed": len(impact.changed_files),
                "agents_to_rerun": impact.agents_to_rerun,
                "new_services": impact.new_services,
                "dependency_changes": impact.dependency_changes,
            },
        })
    except Exception as exc:
        log.exception("Re-assessment failed for %s", managed["repo_name"])
        await s.log_event("github-webhook", "reassessment-failed", managed["repo_name"],
                           "warning", f"Re-assessment failed: {str(exc)[:200]}")
        return JSONResponse({"status": "error", "reason": str(exc)[:200]}, status_code=500)


@router.post("/api/webhook/onboard", dependencies=[Depends(verify_internal_token)])
async def webhook_onboard(request: Request):
    """Trigger onboarding via webhook (called by Argo Events Sensor for low-score assessments)."""
    body = await request.json()

    delivery_id = _get_delivery_id(request, body)
    s = await get_store()
    if await s.webhook_already_processed(delivery_id):
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id})

    log.info("webhook_onboard received event: %s", body.get("eventId", "unknown"))

    assessment_id = body.get("correlationId")
    if not assessment_id:
        result = body.get("result") or {}
        details = result.get("details") or {}
        assessment_id = details.get("assessment_id")
    if not assessment_id:
        raise HTTPException(400, "assessment_id not found in event body")

    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(404, f"Assessment {assessment_id} not found")

    # run_onboarding constructs FleetOrchestrator, which is now genuinely
    # async -- await it directly, no more .raw/to_thread bridge needed for
    # this call path.
    try:
        files, orch_summary = await with_timeout(run_onboarding(report, assessment_id, s))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Onboarding failed for assessment %s", assessment_id)
        return JSONResponse(
            {"error": str(exc), "assessment_id": assessment_id},
            status_code=500,
        )

    await s.save_onboarding(assessment_id, files, orchestration=orch_summary)

    has_containerfile = any(
        f["path"].lower() in ("containerfile", "dockerfile") for f in files
    )
    build_result: dict = {}
    build_status = "skipped"
    if has_containerfile:
        from agentit.image_builder import build_app_image
        build_result = await asyncio.to_thread(build_app_image, report.repo_url, report.repo_name)
        build_status = build_result.get("image_ref", build_result.get("error", "unknown"))
        if "error" not in build_result:
            await s.log_event("image-builder", "build-triggered", report.repo_name, "info",
                               f"Building image: {build_result['image_ref']}")

    log.info("webhook_onboard completed for %s: %d files, build=%s", assessment_id, len(files), build_status)
    await s.mark_webhook_processed(delivery_id)
    return JSONResponse({
        "assessment_id": assessment_id,
        "repo_url": report.repo_url,
        "files_generated": len(files),
        "categories": list({f["category"] for f in files}),
        "image_build": build_status,
    })


@router.post("/api/webhook/auto-apply", dependencies=[Depends(verify_internal_token)])
async def webhook_auto_apply(request: Request):
    """Auto-apply manifests if auto-mode is on and LLM classifies as safe."""
    body = await request.json()
    assessment_id = body.get("assessment_id")
    namespace = body.get("namespace", "default")
    if not assessment_id:
        raise HTTPException(400, "assessment_id required")

    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(404, "Assessment not found")

    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(404, "Onboarding not found -- run onboarding first")

    orch = await s.get_orchestration(assessment_id) or {}
    auto_approve = orch.get("auto_approve", False)

    # AutoMode is now genuinely async -- await it directly, no more
    # .raw/to_thread bridge needed for this call path.
    from agentit.automode import AutoMode
    engine = AutoMode(store=s, publisher=None, llm_client=get_llm_client())

    result = await engine.execute(
        assessment_id, files, namespace,
        report.criticality, auto_approve, report.repo_name,
    )

    log.info("auto-apply result for %s: %s -- %s", assessment_id, result["action"], result["reason"])
    status_code = 207 if result["action"] in ("partial_failure", "failed") else 200
    return JSONResponse(result, status_code=status_code)


@router.post("/api/webhook/finding", dependencies=[Depends(verify_internal_token)])
async def webhook_finding(request: Request):
    """Generic finding remediation -- routes to the right agent generator via the dispatcher.

    Accepts: {"app_name": "...", "category": "container", "description": "...", "severity": "high", "source": "trivy"}
    """
    body = await request.json()

    delivery_id = _get_delivery_id(request, body)
    s = await get_store()
    if await s.webhook_already_processed(delivery_id):
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id})

    app_name = body.get("app_name", "unknown")
    category = body.get("category", "")
    description = body.get("description", "")
    severity = body.get("severity", "info")
    source = body.get("source", "webhook")

    if not category:
        raise HTTPException(400, "category required")
    await s.log_event(source, "finding-received", app_name, severity, f"{category}: {description}")
    publish_event("finding-received", app_name, f"{category}: {description}", agent_id=source)

    fleet = await s.get_fleet_data()
    app = next((a for a in fleet if a["repo_name"] == app_name), None)
    if not app:
        await s.mark_webhook_processed(delivery_id)
        return JSONResponse({"status": "accepted", "action": "alert-only", "reason": f"app '{app_name}' not in fleet"})

    # RemediationDispatcher/AutoMode are now genuinely async -- await them
    # directly, no more .raw/to_thread bridge needed for this call path.
    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(s)
    result = await dispatcher.dispatch(app["id"], category, app_name)

    if result.get("error") and not result["files"]:
        await s.log_event("dispatcher", "no-fix-available", app_name, "warning", result["error"])
        await s.mark_webhook_processed(delivery_id)
        return JSONResponse({"status": "accepted", "action": "alert-only", "reason": result["error"]})

    from agentit.automode import AutoMode
    from agentit.events import get_publisher as _gp
    auto = AutoMode(store=s, publisher=_gp(), llm_client=get_llm_client())

    if await auto.is_enabled() and result["files"]:
        namespace = app.get("deploy_namespace", app_name)
        auto_result = await auto.execute(
            app["id"], result["files"], namespace,
            app.get("criticality", "medium"), False, app_name,
            result["agent"],
        )
        await s.log_event("dispatcher", auto_result["action"], app_name, "info",
                           f"Auto-mode {auto_result['action']} for {category}: {auto_result['reason']}")
        await s.mark_webhook_processed(delivery_id)
        return JSONResponse({
            "status": "accepted",
            "action": auto_result["action"],
            "reason": auto_result["reason"],
            "files_generated": len(result["files"]),
            "agent": result["agent"],
        })

    if result["files"]:
        gate_id = await s.create_gate(
            app["id"], f"finding-{category}",
            f"Dispatcher generated {len(result['files'])} fix(es) for '{category}' -- review and approve",
        )
        await s.log_event("dispatcher", "gated", app_name, "info",
                           f"Fix for {category} gated for review (gate {gate_id})")
        await s.mark_webhook_processed(delivery_id)
        return JSONResponse({
            "status": "accepted",
            "action": "gated",
            "gate_id": gate_id,
            "files_generated": len(result["files"]),
            "agent": result["agent"],
        })

    await s.mark_webhook_processed(delivery_id)
    return JSONResponse({"status": "accepted", "action": "alert-only"})


@router.post("/api/webhook/remediate", dependencies=[Depends(verify_internal_token)])
async def webhook_remediate(request: Request):
    """Trigger the full remediation loop asynchronously.

    Returns HTTP 202 with a job_id for polling status.
    """
    body = await request.json()
    repo_url = body.get("repo_url")
    if not repo_url:
        raise HTTPException(400, "repo_url required")

    app_name = body.get("app_name", repo_url.rstrip("/").split("/")[-1].removesuffix(".git"))
    criticality = body.get("criticality", "medium")
    reason = body.get("reason", "webhook trigger")

    from agentit.remediation_loop import RemediationLoop
    from agentit.events import get_publisher

    # RemediationLoop is now genuinely async -- await it directly. start()
    # itself schedules the actual pipeline as a background asyncio Task
    # (see remediation_loop.py) so this route still returns immediately.
    s = await get_store()
    loop = RemediationLoop(store=s, publisher=get_publisher())
    try:
        job_id = await loop.start(repo_url, app_name, criticality, reason, store=s)
    except Exception as exc:
        log.exception("Failed to start remediation loop for %s", app_name)
        return JSONResponse({"outcome": "failed", "error": str(exc)}, status_code=500)

    return JSONResponse({"status": "accepted", "job_id": job_id}, status_code=202)


@router.get("/api/remediation-jobs/{job_id}")
async def get_remediation_job(job_id: str):
    """Return the status of a single remediation job."""
    s = await get_store()
    job = await s.get_remediation_job(job_id)
    if job is None:
        raise HTTPException(404, "Remediation job not found")
    return JSONResponse(job)


@router.get("/api/remediation-jobs")
async def list_remediation_jobs(assessment_id: str | None = None):
    """List remediation jobs, optionally filtered by assessment_id."""
    s = await get_store()
    return JSONResponse(await s.list_remediation_jobs(assessment_id))
