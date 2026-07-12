"""Webhook endpoints: /api/webhook/*"""
from __future__ import annotations

import asyncio
import logging
import shutil

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from agentit.cloner import clone_repo
from agentit.portal.helpers import get_llm_client, get_store, publish_event

log = logging.getLogger(__name__)

router = APIRouter()

OPERATION_TIMEOUT = 300


async def _with_timeout(coro, timeout: int = OPERATION_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"Operation timed out after {timeout}s")


def _clone_assess_cleanup(repo_url: str, criticality: str, infra_repo_url: str | None = None):
    from agentit.runner import run_assessment
    repo_path = clone_repo(repo_url)
    try:
        return run_assessment(
            repo_path, repo_url, criticality,
            llm_client=get_llm_client(), infra_repo_url=infra_repo_url,
        )
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


def _run_onboarding(report, assessment_id: str | None = None):
    """Run orchestrated onboarding. Returns (files, orchestration_summary)."""
    import tempfile
    from pathlib import Path
    from agentit.agents.orchestrator import FleetOrchestrator

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        orch = FleetOrchestrator(
            report=report, output_dir=base,
            store=get_store(), assessment_id=assessment_id,
        )
        result = orch.run()

        all_files: list[dict] = []
        for ar in result.agent_results:
            if not ar.success:
                continue
            category_dir = base / ar.category
            for rel_path in ar.files_generated:
                file_path = category_dir / rel_path
                if file_path.is_file():
                    all_files.append(
                        {
                            "category": ar.category,
                            "path": rel_path,
                            "description": rel_path,
                            "content": file_path.read_text(encoding="utf-8"),
                        }
                    )

        orch_summary = {
            "agents": [
                {
                    "name": ar.agent_name,
                    "category": ar.category,
                    "success": ar.success,
                    "files_count": len(ar.files_generated),
                    "error": ar.error,
                }
                for ar in result.agent_results
            ],
            "conflicts": result.conflicts,
            "recommendation": result.recommendation,
            "auto_approve": result.plan.auto_approve,
            "gates": result.gates_created,
        }
        return all_files, orch_summary


@router.post("/api/webhook/assess")
async def webhook_assess(request: Request):
    """Trigger an assessment via webhook. Accepts JSON body: {repo_url, criticality}"""
    body = await request.json()
    repo_url = body.get("repo_url")
    if not repo_url:
        raise HTTPException(400, "repo_url required")
    criticality = body.get("criticality", "medium")
    report = await _with_timeout(asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality))
    assessment_id = get_store().save(report)
    from agentit.events import TOPIC_ASSESSMENTS
    publish_event("assessment-complete", report.repo_name,
                  f"Assessment complete: {report.overall_score:.0f}/100",
                  {"assessment_id": assessment_id, "score": report.overall_score},
                  correlation_id=assessment_id,
                  extra_topic=TOPIC_ASSESSMENTS)
    return JSONResponse({"assessment_id": assessment_id, "overall_score": report.overall_score})


@router.post("/api/webhook/github-push")
async def webhook_github_push(request: Request):
    """Handle GitHub push webhooks -- triggers re-assessment for managed repos."""
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        return JSONResponse({"status": "pong"})
    if event_type != "push":
        return JSONResponse({"status": "ignored", "reason": f"event type '{event_type}' not handled"})

    body = await request.json()
    ref = body.get("ref", "")
    repo_url = body.get("repository", {}).get("html_url", "")
    default_branch = body.get("repository", {}).get("default_branch", "main")

    if ref != f"refs/heads/{default_branch}":
        return JSONResponse({"status": "ignored", "reason": f"push to {ref}, not {default_branch}"})

    if not repo_url:
        raise HTTPException(400, "No repository URL in push payload")

    s = get_store()
    fleet = s.get_fleet_data()
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

    s.log_event("github-webhook", "push-received", managed["repo_name"],
                "info", f"Push by {pusher} (commit {commit_sha}) -- re-assessing")

    criticality = managed.get("criticality", "medium")
    try:
        report = await _with_timeout(
            asyncio.to_thread(_clone_assess_cleanup, repo_url, criticality)
        )
        assessment_id = s.save(report)
        s.log_event("github-webhook", "reassessment-complete", managed["repo_name"],
                    "info", f"Re-assessment complete: {report.overall_score:.0f}/100 (was {managed.get('latest_score', '?')})")
        from agentit.events import TOPIC_ASSESSMENTS as _TOPIC_ASSESSMENTS
        publish_event("assessment-complete", managed["repo_name"],
                      f"Re-assessment: {report.overall_score:.0f}/100",
                      {"assessment_id": assessment_id, "score": report.overall_score},
                      correlation_id=assessment_id,
                      extra_topic=_TOPIC_ASSESSMENTS)
        return JSONResponse({
            "status": "assessed",
            "repo": managed["repo_name"],
            "assessment_id": assessment_id,
            "score": report.overall_score,
            "previous_score": managed.get("latest_score"),
        })
    except Exception as exc:
        log.exception("Re-assessment failed for %s", managed["repo_name"])
        s.log_event("github-webhook", "reassessment-failed", managed["repo_name"],
                    "warning", f"Re-assessment failed: {str(exc)[:200]}")
        return JSONResponse({"status": "error", "reason": str(exc)[:200]})


@router.post("/api/webhook/onboard")
async def webhook_onboard(request: Request):
    """Trigger onboarding via webhook (called by Argo Events Sensor for low-score assessments)."""
    body = await request.json()
    log.info("webhook_onboard received event: %s", body.get("eventId", "unknown"))

    assessment_id = body.get("correlationId")
    if not assessment_id:
        result = body.get("result") or {}
        details = result.get("details") or {}
        assessment_id = details.get("assessment_id")
    if not assessment_id:
        raise HTTPException(400, "assessment_id not found in event body")

    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(404, f"Assessment {assessment_id} not found")

    try:
        files, orch_summary = await _with_timeout(asyncio.to_thread(_run_onboarding, report, assessment_id))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Onboarding failed for assessment %s", assessment_id)
        return JSONResponse(
            {"error": str(exc), "assessment_id": assessment_id},
            status_code=500,
        )

    s.save_onboarding(assessment_id, files, orchestration=orch_summary)

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
            s.log_event("image-builder", "build-triggered", report.repo_name, "info",
                        f"Building image: {build_result['image_ref']}")

    log.info("webhook_onboard completed for %s: %d files, build=%s", assessment_id, len(files), build_status)
    return JSONResponse({
        "assessment_id": assessment_id,
        "repo_url": report.repo_url,
        "files_generated": len(files),
        "categories": list({f["category"] for f in files}),
        "image_build": build_status,
    })


@router.post("/api/webhook/auto-apply")
async def webhook_auto_apply(request: Request):
    """Auto-apply manifests if auto-mode is on and LLM classifies as safe."""
    body = await request.json()
    assessment_id = body.get("assessment_id")
    namespace = body.get("namespace", "default")
    if not assessment_id:
        raise HTTPException(400, "assessment_id required")

    s = get_store()
    report = s.get(assessment_id)
    if report is None:
        raise HTTPException(404, "Assessment not found")

    files = s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(404, "Onboarding not found -- run onboarding first")

    orch = s.get_orchestration(assessment_id) or {}
    auto_approve = orch.get("auto_approve", False)

    from agentit.automode import AutoMode
    engine = AutoMode(store=s, publisher=None, llm_client=get_llm_client())

    result = await asyncio.to_thread(
        engine.execute, assessment_id, files, namespace,
        report.criticality, auto_approve, report.repo_name,
    )

    log.info("auto-apply result for %s: %s -- %s", assessment_id, result["action"], result["reason"])
    return JSONResponse(result)


@router.post("/api/webhook/finding")
async def webhook_finding(request: Request):
    """Generic finding remediation -- routes to the right agent generator via the dispatcher.

    Accepts: {"app_name": "...", "category": "container", "description": "...", "severity": "high", "source": "trivy"}
    """
    body = await request.json()
    app_name = body.get("app_name", "unknown")
    category = body.get("category", "")
    description = body.get("description", "")
    severity = body.get("severity", "info")
    source = body.get("source", "webhook")

    if not category:
        raise HTTPException(400, "category required")

    s = get_store()
    s.log_event(source, "finding-received", app_name, severity, f"{category}: {description}")
    publish_event(source, "finding-received", app_name, severity, f"{category}: {description}")

    fleet = s.get_fleet_data()
    app = next((a for a in fleet if a["repo_name"] == app_name), None)
    if not app:
        return JSONResponse({"status": "accepted", "action": "alert-only", "reason": f"app '{app_name}' not in fleet"})

    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(s)
    result = dispatcher.dispatch(app["id"], category, app_name)

    if result.get("error") and not result["files"]:
        s.log_event("dispatcher", "no-fix-available", app_name, "warning", result["error"])
        return JSONResponse({"status": "accepted", "action": "alert-only", "reason": result["error"]})

    from agentit.automode import AutoMode
    from agentit.events import get_publisher as _gp
    auto = AutoMode(store=s, publisher=_gp())

    if auto.enabled and result["files"]:
        from agentit.portal.cluster_apply import apply_manifests_to_cluster
        namespace = app.get("deploy_namespace", app_name)
        apply_result = await asyncio.to_thread(
            apply_manifests_to_cluster, result["files"], namespace, dry_run=False,
        )
        s.log_event("dispatcher", "auto-applied", app_name, "info",
                    f"Applied {len(apply_result['applied'])} fixes for {category}")
        return JSONResponse({
            "status": "accepted",
            "action": "auto-applied",
            "files_generated": len(result["files"]),
            "applied": len(apply_result["applied"]),
            "agent": result["agent"],
        })

    if result["files"]:
        gate_id = s.create_gate(
            app["id"], f"finding-{category}",
            f"Dispatcher generated {len(result['files'])} fix(es) for '{category}' -- review and approve",
        )
        s.log_event("dispatcher", "gated", app_name, "info",
                    f"Fix for {category} gated for review (gate {gate_id})")
        return JSONResponse({
            "status": "accepted",
            "action": "gated",
            "gate_id": gate_id,
            "files_generated": len(result["files"]),
            "agent": result["agent"],
        })

    return JSONResponse({"status": "accepted", "action": "alert-only"})


@router.post("/api/webhook/remediate")
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

    s = get_store()
    loop = RemediationLoop(store=s, publisher=get_publisher())
    try:
        job_id = loop.start(repo_url, app_name, criticality, reason, store=s)
    except Exception as exc:
        log.exception("Failed to start remediation loop for %s", app_name)
        return JSONResponse({"outcome": "failed", "error": str(exc)}, status_code=500)

    return JSONResponse({"status": "accepted", "job_id": job_id}, status_code=202)


@router.get("/api/remediation-jobs/{job_id}")
async def get_remediation_job(job_id: str):
    """Return the status of a single remediation job."""
    job = get_store().get_remediation_job(job_id)
    if job is None:
        raise HTTPException(404, "Remediation job not found")
    return JSONResponse(job)


@router.get("/api/remediation-jobs")
async def list_remediation_jobs(assessment_id: str | None = None):
    """List remediation jobs, optionally filtered by assessment_id."""
    return JSONResponse(get_store().list_remediation_jobs(assessment_id))
