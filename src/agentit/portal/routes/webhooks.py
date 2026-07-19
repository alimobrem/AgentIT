"""Webhook endpoints: /api/webhook/*"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from agentit.portal.helpers import (
    clone_assess_cleanup, get_store, publish_event, run_onboarding, with_timeout,
)

log = logging.getLogger(__name__)

# Width of the time bucket folded into the no-delivery-id fallback key
# below. Wide enough to still dedup genuine near-simultaneous duplicate
# submissions (accidental double-click, a network-level duplicate, the
# concurrent-request race `test_concurrent_identical_deliveries_run_
# pipeline_once` guards), narrow enough that two later, unrelated calls
# with the same content don't collide for the whole dedup retention
# window (7 days -- see `store.purge_old_data`'s `processed_webhooks`
# cutoff).
_DEDUP_TIME_BUCKET_SECONDS = 60


def _get_delivery_id(request: Request, body: dict) -> str:
    """Get a unique delivery ID from GitHub header or body hash.

    The pure content-hash fallback (no time component) used to mean two
    genuinely distinct, unrelated events with an identical body collapsed
    into the same dedup key for the whole `processed_webhooks` retention
    window -- not just a theoretical edge case here: `/api/webhook/assess`
    is called with the exact same `{repo_url, criticality}` body on every
    Tekton CI run for a given app (`chart/templates/tekton/pipeline.yaml`'s
    `register-self-in-fleet` step) and on every `RemediationLoop._assess()`
    call for a given app/criticality (`remediation_loop.py`) -- neither
    caller ever supplies a delivery-id header. Confirmed live-shaped bug:
    a second, legitimate trigger for the same app+criticality within the
    window got silently treated as a duplicate of the first, and
    `RemediationLoop.trigger()` then raised an unhandled `KeyError` trying
    to read `assessment_id` out of the resulting `{"status": "duplicate"}`
    response instead of a real assessment result.
    Folding in a coarse time bucket keeps near-simultaneous duplicate
    POSTs deduped while letting later, content-identical-but-unrelated
    calls each go through. (`/api/webhook/github-push` always carries a
    real GitHub `X-GitHub-Delivery` header in production, and the
    Argo-Events-sourced `/api/webhook/onboard`/`/finding` bodies already
    carry a genuinely unique `eventId` from `EventPublisher.publish()`
    -- see events.py -- so this fallback path is exercised by
    `/api/webhook/assess` in practice, not those routes.)
    """
    gh_delivery = request.headers.get("X-GitHub-Delivery")
    if gh_delivery:
        return gh_delivery
    time_bucket = int(time.time() // _DEDUP_TIME_BUCKET_SECONDS)
    payload = f"{time_bucket}:{json.dumps(body, sort_keys=True)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


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
    if not await s.claim_webhook(delivery_id):
        return JSONResponse({"status": "duplicate", "delivery_id": delivery_id})

    repo_url = body.get("repo_url")
    if not repo_url:
        raise HTTPException(400, "repo_url required")
    criticality = body.get("criticality", "medium")
    check_results: list[dict] = []
    secret_decisions: list[dict] = []
    try:
        report = await with_timeout(
            asyncio.to_thread(
                clone_assess_cleanup, repo_url, criticality,
                check_results_out=check_results, secret_decisions_out=secret_decisions,
            )
        )
    except Exception:
        from agentit.portal.metrics import assessments_total as _at
        _at.labels(criticality=criticality, status="error").inc()
        raise
    from agentit.portal.metrics import assessments_total as _at
    _at.labels(criticality=criticality, status="success").inc()
    assessment_id = await s.save(report)
    await s.save_check_results(assessment_id, check_results)
    from agentit.llm_decisions import build_secret_classify_events
    for ev in build_secret_classify_events(secret_decisions, report.repo_name):
        await s.log_event(**ev, correlation_id=assessment_id)
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

    body_bytes = await request.body()
    if not _verify_github_signature(request, body_bytes):
        raise HTTPException(403, "Invalid webhook signature")

    body = json.loads(body_bytes)

    delivery_id = _get_delivery_id(request, body)
    s_dedup = await get_store()
    if not await s_dedup.claim_webhook(delivery_id):
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
    secret_decisions: list[dict] = []
    try:
        report = await with_timeout(
            asyncio.to_thread(
                clone_assess_cleanup, repo_url, criticality,
                check_results_out=check_results, secret_decisions_out=secret_decisions,
            )
        )
        assessment_id = await s.save(report)
        await s.save_check_results(assessment_id, check_results)
        from agentit.llm_decisions import build_secret_classify_events
        for ev in build_secret_classify_events(secret_decisions, report.repo_name):
            await s.log_event(**ev, correlation_id=assessment_id)
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

                # Finding-scoped re-verification (docs/onboarding-loop-
                # vision-gap-analysis.md Phase 3): every push-triggered
                # re-assessment automatically checks whether any prior
                # delivery on this app that recorded a target finding
                # actually cleared it -- pure correlation + a visible
                # signal, not itself an automated delivery. AutoMode has
                # been removed, so Phase 4's reaction to a confirmed
                # still-present finding is no longer a bounded auto-retry
                # (that retry's own terminal action was an unreviewed
                # auto-delivery) -- it now always escalates straight to a
                # human-review gate, same as the diff.auto_fixable
                # dispatch loop below.
                from agentit.portal.delivery import check_pending_delivery_verifications
                await check_pending_delivery_verifications(s, managed["repo_name"], report, assessment_id)

                # Auto-fix new findings that are auto-fixable. RemediationDispatcher
                # is now genuinely async -- await it directly, no more
                # .raw/to_thread bridge needed for this call path.
                if diff.auto_fixable:
                    from agentit.remediation.dispatcher import RemediationDispatcher
                    dispatcher = RemediationDispatcher(s)
                    for finding in diff.auto_fixable:
                        if await s.get_rejection_count(managed["repo_name"], finding.category) >= 3:
                            await s.log_event("learning", "skipped-rejected", managed["repo_name"],
                                               "info", f"Skipping {finding.category} -- rejected 3+ times")
                            continue
                        dispatch_result = await dispatcher.dispatch(assessment_id, finding.category, managed["repo_name"])
                        if dispatch_result.get("error") and not dispatch_result["files"]:
                            await s.log_event("dispatcher", "no-fix-available", managed["repo_name"],
                                               "warning", dispatch_result["error"])
                            continue
                        if not dispatch_result["files"]:
                            continue
                        # The "fix-generated" event below is the durable
                        # record that a fix was generated, not just an
                        # in-memory dict this handler used to discard once
                        # dispatch() returned -- the real fix/PR outcome is
                        # tracked by the delivery this branch triggers next
                        # (`deliveries`/`gates`, see pr_tracking.py), not a
                        # separate hand-maintained `remediations` row.
                        await s.log_event(
                            "dispatcher", "fix-generated", managed["repo_name"], "info",
                            f"Generated {len(dispatch_result['files'])} fix(es) for '{finding.category}' via {dispatch_result['agent']}",
                        )
                        # AutoMode has been removed: this fix is always
                        # gated for human review now, exactly like
                        # webhook_finding()'s dispatcher branch below --
                        # nothing auto-delivers without an explicit human
                        # action anymore, so a generated fix always stops
                        # here rather than autonomously opening a PR.
                        gate_id = await s.create_gate(
                            assessment_id, f"finding-{finding.category}",
                            f"Dispatcher generated {len(dispatch_result['files'])} fix(es) for "
                            f"'{finding.category}' -- review and approve",
                        )
                        await s.log_event(
                            "dispatcher", "gated", managed["repo_name"], "info",
                            f"Fix for {finding.category} gated for review (gate {gate_id})",
                        )

        await s.log_event("change-analysis", "impact-analyzed", managed["repo_name"],
                           "info", impact.summary())

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
    if not await s.claim_webhook(delivery_id):
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
    return JSONResponse({
        "assessment_id": assessment_id,
        "repo_url": report.repo_url,
        "files_generated": len(files),
        "categories": list({f["category"] for f in files}),
        "image_build": build_status,
    })


@router.post("/api/webhook/auto-apply", dependencies=[Depends(verify_internal_token)])
async def webhook_auto_apply(request: Request):
    """Gate onboarding's generated manifests for human review.

    AutoMode (which used to decide here whether to auto-deliver via an LLM
    safety classification) has been removed: delivery now always requires
    an explicit human action -- the Deliver button on Onboard Results, or
    approving the gate this creates -- consistent with every other former
    AutoMode call site. (This route is only reached via RemediationLoop's
    own pipeline, itself only reachable today via a direct call to
    /api/webhook/remediate; it's kept, unreachable from any autonomous
    trigger, for that explicit-invocation case.)
    """
    body = await request.json()
    assessment_id = body.get("assessment_id")
    if not assessment_id:
        raise HTTPException(400, "assessment_id required")

    s = await get_store()
    report = await s.get(assessment_id)
    if report is None:
        raise HTTPException(404, "Assessment not found")

    files = await s.get_onboarding(assessment_id)
    if files is None:
        raise HTTPException(404, "Onboarding not found -- run onboarding first")

    gate_id = await s.create_gate(
        assessment_id, "auto-mode-review",
        f"Generated {len(files)} manifest(s) for {report.repo_name} -- review and deliver from Onboard Results.",
    )
    result = {
        "action": "gated",
        "reason": "Delivery now always requires human review -- open Onboard Results to Deliver.",
        "details": {"gate_id": gate_id},
    }
    log.info("auto-apply result for %s: %s -- %s", assessment_id, result["action"], result["reason"])
    return JSONResponse(result)


@router.post("/api/webhook/finding", dependencies=[Depends(verify_internal_token)])
async def webhook_finding(request: Request):
    """Generic finding remediation -- routes to the right agent generator via the dispatcher.

    Accepts: {"app_name": "...", "category": "container", "description": "...", "severity": "high", "source": "trivy"}
    """
    body = await request.json()

    delivery_id = _get_delivery_id(request, body)
    s = await get_store()
    if not await s.claim_webhook(delivery_id):
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
        return JSONResponse({"status": "accepted", "action": "alert-only", "reason": f"app '{app_name}' not in fleet"})

    # RemediationDispatcher is now genuinely async -- await it directly,
    # no more .raw/to_thread bridge needed for this call path.
    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(s)
    result = await dispatcher.dispatch(app["id"], category, app_name)

    if result.get("error") and not result["files"]:
        await s.log_event("dispatcher", "no-fix-available", app_name, "warning", result["error"])
        return JSONResponse({"status": "accepted", "action": "alert-only", "reason": result["error"]})

    # AutoMode has been removed: a generated fix always gates for human
    # review now -- nothing auto-delivers without an explicit human action
    # anymore.
    if result["files"]:
        gate_id = await s.create_gate(
            app["id"], f"finding-{category}",
            f"Dispatcher generated {len(result['files'])} fix(es) for '{category}' -- review and approve",
        )
        await s.log_event("dispatcher", "gated", app_name, "info",
                           f"Fix for {category} gated for review (gate {gate_id})")
        return JSONResponse({
            "status": "accepted",
            "action": "gated",
            "gate_id": gate_id,
            "files_generated": len(result["files"]),
            "agent": result["agent"],
        })

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


@router.post("/api/webhook/skill-draft", dependencies=[Depends(verify_internal_token)])
async def webhook_skill_draft(request: Request):
    """Internal-only endpoint so the ``skill-learner`` watcher's drafts land
    in the exact same place the portal's own in-process "Research CVEs &
    Generate Skills" button (``capabilities.py``'s ``capabilities_learn_route``)
    already writes to, and that ``capabilities.py``'s ``_cached_skills()``
    already scans -- making them visible on the Capabilities page with no
    pod restart or manual sync step.

    The watcher runs in its own Deployment with no shared filesystem with
    the portal, and no ReadWriteMany storage class is available on this
    cluster (confirmed via `oc get storageclass` -- only gp2-csi/gp3-csi,
    both EBS-backed/ReadWriteOnce), so a watcher-drafted skill was
    previously only ever visible via `oc exec` into that one pod. This
    mirrors the exact same ``AGENTIT_PORTAL_URL`` + internal-token pattern
    ``RemediationLoop`` already uses (``remediation_loop.py``) to call back
    into the portal from a separate watcher pod, and the
    ``/api/webhook/*`` + ``verify_internal_token`` convention every other
    in-cluster-only route above already follows.

    Accepts JSON ``{"content": "<skill markdown>", "domain": "security"}``.
    """
    body = await request.json()
    content = body.get("content", "")
    domain = body.get("domain", "security")
    if not content:
        raise HTTPException(400, "content required")

    from agentit.learning_agent import save_skill
    path = await asyncio.to_thread(save_skill, content, Path("skills"), domain=domain)
    if path is None:
        raise HTTPException(500, "Failed to save skill draft")

    # Bust the Capabilities page's 60s skills cache so this draft shows up
    # on the very next page load -- same cache `capabilities_learn_route`
    # busts after a manual "Research CVEs & Generate Skills" run.
    from agentit.portal.routes import capabilities as _capabilities
    _capabilities._skills_cache["data"] = None

    return JSONResponse({"status": "saved", "name": path.stem, "path": str(path)})


@router.post("/api/webhook/synthetic-probe", dependencies=[Depends(verify_internal_token)])
async def webhook_synthetic_probe(request: Request):
    """Reports an external synthetic uptime probe result against the public
    Route, plus the TLS certificate expiry it observed while doing so.

    Accepts JSON: {"up": bool, "latency_ms": number, "cert_days_remaining": number | null}.
    Called by the agentit-synthetic-probe CronJob
    (chart/templates/synthetic-probe-cronjob.yaml) -- see its module comment
    for why this check has to run from outside the pod's own Service (it's
    the one class of failure kubelet's /healthz-based liveness/readiness
    probes structurally can't see: Route/router-level failures).
    """
    import time as _time
    body = await request.json()
    up = bool(body.get("up"))
    cert_days = body.get("cert_days_remaining")

    from agentit.portal.metrics import (
        synthetic_probe_up, synthetic_probe_last_run_timestamp, route_cert_expiry_days,
    )
    synthetic_probe_up.set(1 if up else 0)
    synthetic_probe_last_run_timestamp.set(_time.time())
    if cert_days is not None:
        route_cert_expiry_days.set(float(cert_days))

    s = await get_store()
    if not up:
        await s.log_event("synthetic-probe", "probe-failed", "agentit", "warning",
                           f"External synthetic probe against the public Route failed: {body.get('detail', 'no detail')}")
    return JSONResponse({"status": "recorded", "up": up, "cert_days_remaining": cert_days})


@router.post("/api/webhook/backup-status", dependencies=[Depends(verify_internal_token)])
async def webhook_backup_status(request: Request):
    """Reports whether a backup CronJob run succeeded, so a silent backup
    failure shows up as a stale/failed Prometheus gauge instead of only as a
    line in a CronJob pod's logs nobody is tailing.

    Accepts JSON: {"target": "sqlite" | "postgres", "status": "ok" | "fail", "detail": str}.
    Called by db-backup-cronjob.yaml and postgres-bundled-backup.yaml.
    """
    import time as _time
    body = await request.json()
    target = body.get("target", "unknown")
    ok = body.get("status") == "ok"

    from agentit.portal.metrics import backup_last_status, backup_last_success_timestamp
    backup_last_status.labels(target=target).set(1 if ok else 0)
    if ok:
        backup_last_success_timestamp.labels(target=target).set(_time.time())

    s = await get_store()
    detail = body.get("detail", "")
    if ok:
        await s.log_event("backup", "backup-succeeded", "agentit", "info", f"{target} backup succeeded. {detail}".strip())
    else:
        await s.log_event("backup", "backup-failed", "agentit", "warning", f"{target} backup failed. {detail}".strip())
    return JSONResponse({"status": "recorded", "target": target, "ok": ok})


@router.post("/api/webhook/secret-check", dependencies=[Depends(verify_internal_token)])
async def webhook_secret_check(request: Request):
    """Reports whether a security-sensitive Secret still exists on-cluster --
    a drift check, not a rotation. Exists specifically to prevent a repeat of
    the 2026-07-13 incident (docs/deployment.md): github-webhook-secret went
    missing/unset for ~8.5 hours with nothing surfacing it until a human
    happened to investigate a symptom. This can't verify the secret's value
    matches what's configured on GitHub's side (that would require holding a
    GitHub token capable of reading webhook config), only that it exists at
    all in-cluster -- see secret-rotation-cronjob.yaml's module comment for
    the full reasoning.

    Accepts JSON: {"secret": str, "exists": bool}.
    """
    import time as _time
    body = await request.json()
    secret = body.get("secret", "unknown")
    exists = bool(body.get("exists"))

    from agentit.portal.metrics import secret_check_status, secret_check_last_run_timestamp
    secret_check_status.labels(secret=secret).set(1 if exists else 0)
    secret_check_last_run_timestamp.labels(secret=secret).set(_time.time())

    if not exists:
        s = await get_store()
        await s.log_event("secret-check", "secret-missing", "agentit", "critical",
                           f"Secret '{secret}' is missing from the cluster -- dependent webhooks/integrations will fail.")
    return JSONResponse({"status": "recorded", "secret": secret, "exists": exists})


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
