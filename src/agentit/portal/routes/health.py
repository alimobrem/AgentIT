"""Health / system status endpoints."""
from __future__ import annotations

import asyncio
import html
import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agentit import kube
from agentit.portal import github_pr
from agentit.portal.health_links import (
    build_health_card_links,
    enrich_argo_apps_with_links,
    enrich_pipelines_with_links,
    resolve_console_url,
    resolve_github_repo_url,
)
from agentit.portal.helpers import get_circuit_breaker_states, get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()

# Ambient /api/deploy-status is polled every 15s from every page. Keep kube
# calls short and cache last-good so a wedged apiserver cannot pin workers
# until oauth-proxy returns 502/503 awaiting response headers.
_DEPLOY_STATUS_K8S_TIMEOUT = 2
_DEPLOY_STATUS_DEADLINE = 3.0
_DEPLOY_STATUS_CACHE_TTL = 20.0
_deploy_status_cache: dict = {"data": None, "ts": 0.0}
# `_get_deploy_status_bounded` runs in a real OS thread (via
# `asyncio.to_thread`), so concurrent pollers can genuinely interleave the
# read-check-write below at the bytecode level -- an `asyncio.Lock` would
# not help here since it only excludes other coroutines on the same event
# loop thread, not other OS threads.
_deploy_status_cache_lock = threading.Lock()


# ── Internal helpers ──────────────────────────────────────────────────
#
# All helpers below swallow every exception and return None/[] on failure
# (missing resource, unreachable cluster, RBAC denial, ...) so route handlers
# can treat "couldn't get it" as a single case, matching the previous
# subprocess-based behavior where any `oc` failure produced no stdout.


def _get_pod(name: str, namespace: str = "agentit"):
    try:
        return kube.core_v1().read_namespaced_pod(name, namespace, _request_timeout=10)
    except Exception:
        log.debug("Failed to read pod %s/%s", namespace, name, exc_info=True)
        return None


def _get_pod_log(name: str, namespace: str = "agentit", tail_lines: int = 100, timeout: int = 15) -> str | None:
    try:
        return kube.core_v1().read_namespaced_pod_log(
            name, namespace, tail_lines=tail_lines, _request_timeout=timeout,
        )
    except Exception:
        log.debug("Failed to read pod log %s/%s", namespace, name, exc_info=True)
        return None


def _get_all_container_logs(name: str, namespace: str = "agentit", tail_lines: int = 50) -> str | None:
    """Read and concatenate logs from every container in a pod (like `oc logs --all-containers`)."""
    try:
        pod = kube.core_v1().read_namespaced_pod(name, namespace, _request_timeout=10)
    except Exception:
        log.debug("Failed to read pod %s/%s for all-container logs", namespace, name, exc_info=True)
        return None

    parts = []
    for c in pod.spec.containers or []:
        try:
            text = kube.core_v1().read_namespaced_pod_log(
                name, namespace, container=c.name, tail_lines=tail_lines, _request_timeout=15,
            )
            parts.append(f"[{c.name}]\n{text}")
        except Exception:
            log.debug("Failed to read log for container %s in pod %s/%s", c.name, namespace, name, exc_info=True)
    return "\n".join(parts) if parts else None


def _get_pod_events(name: str, namespace: str = "agentit") -> list:
    try:
        events = kube.core_v1().list_namespaced_event(
            namespace, field_selector=f"involvedObject.name={name}", _request_timeout=10,
        )
        return events.items or []
    except Exception:
        log.debug("Failed to list events for pod %s/%s", namespace, name, exc_info=True)
        return []


def _get_custom_resource_safe(group: str, version: str, plural: str, name: str, namespace: str) -> dict | None:
    try:
        return kube.get_custom_resource(group, version, plural, name, namespace=namespace)
    except Exception:
        log.debug("Failed to get %s/%s %s/%s in %s", group, version, plural, name, namespace, exc_info=True)
        return None


def _taskrun_status(name: str, namespace: str = "agentit") -> str:
    """Real status for a single Tekton TaskRun by name.

    Verified live against a real cluster: Tekton v1 ``PipelineRun.status.
    childReferences`` entries carry only ``name``/``kind``/``pipelineTaskName``
    -- never an embedded ``conditions`` array (that was a v1beta1-only field,
    since removed) -- so a task's actual running/done/failed state has to be
    read off its own TaskRun object, one ``get`` per child. Without this, a
    naive read of ``childReferences[].conditions`` (as ``pipeline_detail_page``
    above still does) always reports "Unknown" for every task on this
    cluster's Tekton version, regardless of real progress.
    """
    try:
        tr = kube.get_custom_resource(
            "tekton.dev", "v1", "taskruns", name,
            namespace=namespace, timeout=_DEPLOY_STATUS_K8S_TIMEOUT,
        )
        if tr is None:
            return "Pending"
        conditions = tr.get("status", {}).get("conditions", [{}])
        return conditions[0].get("reason", "Unknown") if conditions else "Unknown"
    except Exception:
        log.debug("Failed to read TaskRun %s for deploy status", name, exc_info=True)
        return "Unknown"


def _degraded_deploy_status(errors: list[str] | None = None) -> dict:
    """Fallback when kube calls time out or fail -- never hang the badge."""
    from agentit.portal.metrics import get_build_info

    return {
        "running": get_build_info(),
        "pipeline": None,
        "argo": None,
        "commit_info": None,
        "state": "idle",
        "stage": None,
        "reason": None,
        "resolved": None,
        "errors": list(errors or ["Deploy status unavailable"]),
        "degraded": True,
    }


def _get_fresh_cached_deploy_status() -> dict | None:
    with _deploy_status_cache_lock:
        cached = _deploy_status_cache["data"]
        if cached is None:
            return None
        if (time.monotonic() - _deploy_status_cache["ts"]) >= _DEPLOY_STATUS_CACHE_TTL:
            return None
        return cached


def _get_last_good_deploy_status() -> dict | None:
    with _deploy_status_cache_lock:
        return _deploy_status_cache["data"]


def _store_deploy_status_cache(status: dict) -> None:
    """Cache last-good only (no errors) so degraded polls can fall back."""
    if status.get("errors") or status.get("degraded"):
        return
    with _deploy_status_cache_lock:
        _deploy_status_cache["data"] = status
        _deploy_status_cache["ts"] = time.monotonic()


def _get_deploy_status_bounded(include_commit_info: bool = False) -> dict:
    """Cache-aware deploy status for the ambient badge (no GitHub enrichment).

    Serves a fresh cache hit within ``_DEPLOY_STATUS_CACHE_TTL`` to cut
    apiserver load from htmx polling; on API errors prefers last-good over
    a blank degraded response when available.
    """
    if not include_commit_info:
        cached = _get_fresh_cached_deploy_status()
        if cached is not None:
            return cached

    status = _get_deploy_status(include_commit_info=include_commit_info)

    if not include_commit_info:
        if not status.get("errors"):
            _store_deploy_status_cache(status)
        else:
            last_good = _get_last_good_deploy_status()
            if last_good is not None:
                out = dict(last_good)
                out["errors"] = list(status.get("errors") or []) + [
                    "Serving last-good deploy status",
                ]
                out["degraded"] = True
                return out
    return status


def _get_deploy_status(include_commit_info: bool = False) -> dict:
    """"Is a deploy in progress, and what's changing" -- combines the
    currently-running build (`agentit_build` Info metric, via
    ``get_build_info()``) with the live ``agentit-ci`` PipelineRun and the
    live ``agentit`` Argo CD Application. Backs both the ambient nav badge
    (``/api/deploy-status``, ``include_commit_info=False`` -- no GitHub call
    on every poll) and the Health page's detailed section
    (``include_commit_info=True``).

    Every field is either real data from a live API call or ``None``/absent
    on failure -- an unreachable PipelineRun/Application API is reported via
    ``errors``, never silently treated as "nothing in progress".
    """
    from agentit.portal.metrics import get_build_info

    status: dict = {
        "running": get_build_info(),
        "pipeline": None,
        "argo": None,
        "commit_info": None,
        "state": "idle",  # "idle" | "deploying" | "failed"
        "stage": None,
        "reason": None,
        "resolved": None,
        "errors": [],
    }

    latest_run = None
    try:
        runs = kube.list_custom_resources(
            "tekton.dev", "v1", "pipelineruns",
            namespace="agentit", timeout=_DEPLOY_STATUS_K8S_TIMEOUT,
        )
        ci_runs = [
            r for r in runs
            if r.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline") == "agentit-ci"
        ]
        # list_custom_resources order is not guaranteed -- pick newest by time.
        ci_runs.sort(key=lambda r: r.get("metadata", {}).get("creationTimestamp") or "")
        latest_run = ci_runs[-1] if ci_runs else None
    except Exception:
        log.warning("Failed to list agentit-ci PipelineRuns for deploy status", exc_info=True)
        status["errors"].append("Could not reach the Tekton PipelineRun API")

    if latest_run is not None:
        conditions = latest_run.get("status", {}).get("conditions", [{}])
        cond = conditions[0] if conditions else {}
        reason = cond.get("reason", "Unknown")
        is_running = cond.get("status", "Unknown") == "Unknown"
        revision = next(
            (p.get("value", "") for p in latest_run.get("spec", {}).get("params", []) if p.get("name") == "revision"),
            "",
        )
        tasks = []
        for child in latest_run.get("status", {}).get("childReferences", []):
            child_name = child.get("name", "")
            task_reason = _taskrun_status(child_name) if child_name else "Unknown"
            tasks.append({"name": child.get("pipelineTaskName", child_name or "?"), "status": task_reason})
        status["pipeline"] = {
            "name": latest_run.get("metadata", {}).get("name", "?"),
            "reason": reason,
            "running": is_running,
            "revision": revision,
            "tasks": tasks,
        }
        if is_running:
            status["state"] = "deploying"
            status["stage"] = next((t["name"] for t in tasks if t["status"] != "Succeeded"), "starting")
        elif reason in ("Cancelled", "PipelineRunCancelled"):
            # Cancelled runs are operator/capacity noise (concurrent CI cancelled
            # to free the node), not a deploy failure. Surface the run but leave
            # state idle so Argo/Rollout decide the real outcome.
            pass
        elif reason not in ("Succeeded", "Completed"):
            status["state"] = "failed"
            status["reason"] = f"CI pipeline {reason}"

    agentit_app = None
    try:
        apps = kube.list_custom_resources(
            "argoproj.io", "v1alpha1", "applications",
            namespace="openshift-gitops", timeout=_DEPLOY_STATUS_K8S_TIMEOUT,
        )
        agentit_app = next((a for a in apps if a.get("metadata", {}).get("name") == "agentit"), None)
    except Exception:
        log.warning("Failed to fetch the agentit Argo CD Application for deploy status", exc_info=True)
        status["errors"].append("Could not reach the Argo CD Application API")

    if agentit_app is not None:
        sync = agentit_app.get("status", {}).get("sync", {})
        health = agentit_app.get("status", {}).get("health", {})
        sync_status = sync.get("status", "Unknown")
        health_status = health.get("status", "Unknown")
        params = agentit_app.get("spec", {}).get("source", {}).get("helm", {}).get("parameters", [])
        image_tag = next((p.get("value", "") for p in params if p.get("name") == "image.tag"), "")
        op_phase = (agentit_app.get("status", {}).get("operationState") or {}).get("phase", "")
        status["argo"] = {
            "sync": sync_status,
            "health": health_status,
            "health_message": health.get("message", ""),
            "image_tag": image_tag,
            "repo_url": agentit_app.get("spec", {}).get("source", {}).get("repoURL", ""),
            "operation_phase": op_phase,
        }
        if status["state"] != "deploying":
            # Active sync/hook work is "deploying", not Failed -- Degraded is
            # often transient (PDB SyncFailed while a Job matched the selector,
            # or a Sync hook Pending) while operationState is still Running.
            if op_phase == "Running" or health_status in ("Progressing", "Suspended"):
                # Suspended = canary pause step (Rollout), still an in-flight deploy.
                status["state"] = "deploying"
                if op_phase == "Running":
                    status["stage"] = "syncing"
                elif health_status == "Suspended":
                    status["stage"] = "canary pause"
                else:
                    status["stage"] = "rolling out"
            elif health_status == "Degraded":
                status["state"] = "failed"
                status["reason"] = status["argo"]["health_message"] or "Argo CD reports the agentit Application as Degraded"
            elif sync_status != "Synced":
                status["state"] = "deploying"
                status["stage"] = "syncing"

    # Resolved outcome: once the latest PipelineRun succeeded and nothing is
    # actively deploying/failed, check whether this running instance's own
    # commit (its real, live env, not a guess) actually matches that
    # PipelineRun's target revision -- catches a canary that got aborted /
    # rolled back after the pipeline itself succeeded.
    if status["state"] == "idle" and status["pipeline"] and status["pipeline"]["reason"] == "Succeeded":
        running_commit = status["running"].get("commit", "unknown")
        revision = status["pipeline"]["revision"]
        if running_commit not in ("unknown", "") and revision:
            if running_commit == revision or running_commit.startswith(revision) or revision.startswith(running_commit):
                status["resolved"] = {
                    "outcome": "healthy",
                    "message": "This instance is running the version built by the latest PipelineRun.",
                }
            else:
                msg = (
                    f"This instance is running commit {running_commit[:12]}, not the latest "
                    f"PipelineRun's target ({revision[:12]}) -- likely rolled back."
                )
                if status["argo"] and status["argo"].get("health_message"):
                    msg += f" Argo CD: {status['argo']['health_message']}"
                status["resolved"] = {"outcome": "rolled_back", "message": msg}

    if include_commit_info and status["pipeline"] and status["argo"]:
        revision = status["pipeline"].get("revision")
        repo_url = status["argo"].get("repo_url")
        if revision and repo_url:
            try:
                status["commit_info"] = github_pr.get_commit_info(repo_url, revision) or None
            except Exception:
                log.warning("Failed to fetch commit info for deploy status", exc_info=True)

    return status


def _get_cluster_health(store=None, loop=None) -> dict:
    """Runs off the event loop via ``asyncio.to_thread`` at every call site,
    so ``store``'s coroutine methods need bridging back onto ``loop`` (the
    event loop that constructed the store) via
    ``asyncio.run_coroutine_threadsafe`` -- the same pattern
    ``EventConsumer._persist_dead_letter`` uses for the identical
    constraint (an ``asyncpg`` pool is bound to its creating loop and can't
    be driven from a different thread's loop)."""
    from agentit import kube

    import os
    result: dict = {
        "argo_apps": [], "argo_synced": False,
        "pods": [], "pods_running": 0, "pods_failed": 0,
        "pipelines": [], "pipeline_status": "Unknown",
        "kafka_ready": False, "publisher_ok": False,
        "namespace": os.environ.get("AGENTIT_NAMESPACE", "agentit"),
        "cluster_url": os.environ.get("KUBERNETES_SERVICE_HOST", "local"),
        "latest_pipeline_name": None,
        "last_successful_ci_name": None,
        "current_commit": "",
        "current_commit_full": "",
        "github_repo_url": None,
        "console_url": None,
        "kafka_name": None,
        "card_links": {},
    }

    managed_names = {"agentit"}
    try:
        if store is not None:
            fleet_data = store.get_fleet_data()
            if asyncio.iscoroutine(fleet_data):
                fleet_data = asyncio.run_coroutine_threadsafe(fleet_data, loop).result(timeout=30)
            for app_data in fleet_data:
                managed_names.add(app_data["repo_name"].lower().replace("_", "-").replace(".", "-"))
    except Exception:
        log.debug("Failed to load fleet for health check", exc_info=True)

    # Argo CD apps
    try:
        argo_items = kube.list_custom_resources(
            "argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops",
        )
        for a in argo_items:
            name = a.get("metadata", {}).get("name", "?")
            bare_name = name.removeprefix("managed-")
            if name not in managed_names and bare_name not in managed_names:
                continue
            sync = a.get("status", {}).get("sync", {}).get("status", "Unknown")
            health = a.get("status", {}).get("health", {}).get("status", "Unknown")
            result["argo_apps"].append({"name": name, "sync": sync, "health": health})
        result["argo_synced"] = all(a["sync"] == "Synced" for a in result["argo_apps"]) if result["argo_apps"] else None
    except Exception:
        log.warning("Failed to fetch Argo CD apps", exc_info=True)
        result["argo_synced"] = None

    # Pods
    try:
        pods = kube.list_pods("agentit")
        for p in pods:
            if p["status"] in ("Succeeded", "Completed"):
                continue
            # A Tekton TaskRun-owned pod that reached "Failed" is a one-shot
            # execution record (e.g. a single onboarding/build attempt), not
            # an ongoing service health signal -- it will never restart or
            # recover, and its outcome is already surfaced separately via
            # `pipeline_status`/`pipelines` above for CI runs. Without this,
            # a single historical build failure pins `pods_failed` (and the
            # "Platform" card) at Degraded forever, even though nothing
            # about the running application is actually broken. Verified
            # live: a stale `*-git-clone-pod` from an unrelated onboarding
            # attempt kept /health reporting Degraded for hours after the
            # attempt itself had already finished.
            if p["status"] == "Failed" and p.get("owner_kind") == "TaskRun":
                continue
            result["pods"].append({
                "name": p["name"], "status": p["status"],
                "restarts": p["restarts"], "age": p["age"],
            })
        result["pods_running"] = sum(1 for p in result["pods"] if p["status"] == "Running")
        result["pods_failed"] = sum(1 for p in result["pods"] if p["status"] in ("Failed", "Error", "CrashLoopBackOff"))
    except Exception:
        log.warning("Failed to list pods", exc_info=True)

    # Pipeline runs
    try:
        all_runs = kube.list_custom_resources(
            "tekton.dev", "v1", "pipelineruns", namespace="agentit",
        )
        all_runs = [
            r for r in all_runs
            if r.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline") == "agentit-ci"
        ]
        runs = all_runs[-5:]
        for r in runs:
            name = r.get("metadata", {}).get("name", "?")
            conditions = r.get("status", {}).get("conditions", [{}])
            status = conditions[0].get("reason", "Unknown") if conditions else "Unknown"
            start = r.get("status", {}).get("startTime", "")
            completion = r.get("status", {}).get("completionTime", "")
            duration = ""
            if start and completion:
                duration = f"{start[:16]} -> {completion[:16]}"
            elif start:
                duration = f"Started {start[:16]}"
            result["pipelines"].append({"name": name, "status": status, "duration": duration})
        if result["pipelines"]:
            result["pipeline_status"] = result["pipelines"][-1]["status"]
            result["latest_pipeline_name"] = result["pipelines"][-1]["name"]
        for r in reversed(all_runs):
            conds = r.get("status", {}).get("conditions", [{}])
            if conds and conds[0].get("reason") == "Succeeded":
                ct = r.get("status", {}).get("completionTime", "")
                result["last_successful_ci"] = ct[:19] if ct else "?"
                result["last_successful_ci_name"] = r.get("metadata", {}).get("name")
                break
    except Exception:
        log.warning("Failed to list pipeline runs", exc_info=True)

    # Rollout
    try:
        rollouts = kube.list_custom_resources(
            "argoproj.io", "v1alpha1", "rollouts", namespace="agentit",
        )
        ro = next((r for r in rollouts if r.get("metadata", {}).get("name") == "agentit"), None)
        if ro:
            result["rollout_phase"] = ro.get("status", {}).get("phase", "Unknown")
            result["rollout_step"] = ro.get("status", {}).get("currentStepIndex", 0)
            result["rollout_total_steps"] = len(
                ro.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", [])
            )
    except Exception:
        log.debug("Failed to get rollout", exc_info=True)

    # Current commit from Argo CD application
    try:
        argo_apps = kube.list_custom_resources(
            "argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops",
        )
        agentit_app = next(
            (a for a in argo_apps if a.get("metadata", {}).get("name") == "agentit"), None,
        )
        if agentit_app:
            rev = agentit_app.get("status", {}).get("sync", {}).get("revision", "")
            result["current_commit_full"] = rev
            result["current_commit"] = rev[:12] if rev else ""
            result["github_repo_url"] = resolve_github_repo_url(
                agentit_app.get("spec", {}).get("source", {}).get("repoURL", ""),
            )
        else:
            result["current_commit"] = ""
            result["current_commit_full"] = ""
    except Exception:
        log.debug("Failed to get current commit from Argo CD", exc_info=True)
        result["current_commit"] = ""
        result["current_commit_full"] = ""

    # Kafka
    try:
        kafkas = kube.list_custom_resources("kafka.strimzi.io", "v1beta2", "kafkas", namespace="agentit")
        if kafkas:
            result["kafka_name"] = kafkas[0].get("metadata", {}).get("name")
            conditions = kafkas[0].get("status", {}).get("conditions", [])
            result["kafka_ready"] = any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
            )
    except Exception:
        log.debug("Failed to check Kafka status", exc_info=True)

    try:
        from agentit.events import get_publisher
        pub = get_publisher()
        result["publisher_ok"] = pub._producer is not None
    except Exception:
        log.debug("Failed to check event publisher", exc_info=True)

    try:
        from agentit.events import get_kafka_stats
        kafka_stats = get_kafka_stats()
        result["kafka_stats"] = kafka_stats
    except Exception:
        log.debug("Failed to collect Kafka stats", exc_info=True)
        result["kafka_stats"] = {"available": False, "topics": {}, "consumer_groups": []}

    try:
        result["circuit_breakers"] = get_circuit_breaker_states()
        from agentit.portal.metrics import refresh_circuit_breaker_gauge
        refresh_circuit_breaker_gauge()
    except Exception:
        log.debug("Failed to collect circuit breaker states", exc_info=True)
        result["circuit_breakers"] = {}

    # Deep-links for Health cards / tables — real console/GitHub URLs only.
    console_url = resolve_console_url()
    result["console_url"] = console_url
    result["argo_apps"] = enrich_argo_apps_with_links(result["argo_apps"], console_url)
    result["pipelines"] = enrich_pipelines_with_links(
        result["pipelines"], console_url, result["namespace"],
    )
    result["card_links"] = build_health_card_links(
        console_url=console_url,
        github_repo_url=result.get("github_repo_url"),
        namespace=result["namespace"],
        latest_pipeline_name=result.get("latest_pipeline_name"),
        last_successful_ci_name=result.get("last_successful_ci_name"),
        current_commit=result.get("current_commit_full") or result.get("current_commit"),
        kafka_name=result.get("kafka_name"),
    )

    return result


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> HTMLResponse:
    store = await get_store()
    loop = asyncio.get_running_loop()
    data = await asyncio.to_thread(_get_cluster_health, store, loop)
    data["deploy_status"] = await asyncio.to_thread(_get_deploy_status, True)
    return get_templates().TemplateResponse(request, "health.html", data)


@router.get("/api/deploy-status", response_class=HTMLResponse)
async def deploy_status_badge(request: Request) -> HTMLResponse:
    """Ambient nav badge fragment -- polled by htmx from base.html on every
    page (``hx-trigger="load, every 15s"``) so "what version is running / is
    a deploy in progress right now" stays visible without navigating to
    /health or reloading. Deliberately lightweight: no GitHub call here
    (that only happens for the Health page's detailed section via
    ``_get_deploy_status(include_commit_info=True)``).

    Bounded by ``_DEPLOY_STATUS_DEADLINE`` so a slow/wedged kube-apiserver
    returns **200** with degraded/last-good HTML instead of hanging the
    worker until oauth-proxy times out with 502/503.
    """
    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(_get_deploy_status_bounded),
            timeout=_DEPLOY_STATUS_DEADLINE,
        )
    except asyncio.TimeoutError:
        log.warning(
            "GET /api/deploy-status timed out after %.1fs; returning degraded/last-good",
            _DEPLOY_STATUS_DEADLINE,
        )
        last_good = _get_last_good_deploy_status()
        if last_good is not None:
            status = dict(last_good)
            status["degraded"] = True
            status["errors"] = list(status.get("errors") or []) + [
                "Deploy status timed out waiting for the Kubernetes API; serving last-good",
            ]
        else:
            status = _degraded_deploy_status(
                ["Deploy status timed out waiting for the Kubernetes API"],
            )
    return get_templates().TemplateResponse(request, "deploy_status_badge.html", {"deploy_status": status})


@router.get("/health/pods/{pod_name}", response_class=HTMLResponse)
async def pod_detail_page(request: Request, pod_name: str) -> HTMLResponse:
    pod = await asyncio.to_thread(_get_pod, pod_name)
    if pod is None:
        raise HTTPException(404, f"Pod {pod_name} not found")

    status = pod.status.phase or "Unknown"
    created = pod.metadata.creation_timestamp.isoformat()[:19] if pod.metadata.creation_timestamp else ""
    containers = []
    total_restarts = 0
    for cs in pod.status.container_statuses or []:
        restarts = cs.restart_count or 0
        total_restarts += restarts
        containers.append({
            "name": cs.name or "?",
            "image": cs.image or "?",
            "ready": bool(cs.ready),
            "restarts": restarts,
        })

    logs = await asyncio.to_thread(_get_pod_log, pod_name, "agentit", 100, 15) or "No logs available"

    pod_events = await asyncio.to_thread(_get_pod_events, pod_name)
    events = [
        {
            "time": (e.last_timestamp.isoformat()[:19] if e.last_timestamp else ""),
            "type": e.type or "Normal",
            "reason": e.reason or "?",
            "message": (e.message or "")[:200],
        }
        for e in pod_events
    ]

    return get_templates().TemplateResponse(request, "pod_detail.html", {
        "pod_name": pod_name,
        "status": status,
        "restarts": total_restarts,
        "created": created,
        "containers": containers,
        "logs": logs,
        "events": events,
    })


@router.get("/health/pipelines/{pipeline_name}", response_class=HTMLResponse)
async def pipeline_detail_page(request: Request, pipeline_name: str) -> HTMLResponse:
    pr = await asyncio.to_thread(
        _get_custom_resource_safe, "tekton.dev", "v1", "pipelineruns", pipeline_name, "agentit",
    )
    if pr is None:
        raise HTTPException(404, f"PipelineRun {pipeline_name} not found")

    conditions = pr.get("status", {}).get("conditions", [{}])
    status = conditions[0].get("reason", "Unknown") if conditions else "Unknown"
    start_time = pr.get("status", {}).get("startTime", "")[:19]
    completion_time = (pr.get("status", {}).get("completionTime") or "")[:19]

    tasks = []
    for child in pr.get("status", {}).get("childReferences", []):
        task_name = child.get("pipelineTaskName", child.get("name", "?"))
        conds = child.get("conditions", [])
        task_status = "Unknown"
        if conds:
            task_status = conds[0].get("reason", "Unknown")
        elif child.get("status"):
            task_status = child["status"]
        pod = child.get("name", "")
        tasks.append({"name": task_name, "status": task_status, "pod": pod})

    logs = ""
    if tasks:
        last_pod = tasks[-1].get("pod", "")
        if last_pod:
            logs = await asyncio.to_thread(_get_all_container_logs, last_pod, "agentit", 50) or ""

    return get_templates().TemplateResponse(request, "pipeline_detail.html", {
        "pipeline_name": pipeline_name,
        "status": status,
        "start_time": start_time,
        "completion_time": completion_time,
        "tasks": tasks,
        "logs": logs,
    })


@router.get("/healthz")
async def healthz():
    try:
        s = await get_store()
        await s.get_setting("__healthz_probe__")
    except Exception as exc:
        return JSONResponse({"status": "unhealthy", "error": str(exc)}, status_code=503)
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    try:
        s = await get_store()
        await s.get_setting("__readyz_probe__")
    except Exception as exc:
        return JSONResponse({"status": "not ready", "backend": "postgres", "error": str(exc)}, status_code=503)
    import os
    if os.environ.get("AGENTIT_KAFKA_BOOTSTRAP"):
        from agentit.events import get_publisher
        pub = get_publisher()
        if not pub.kafka_enabled:
            return JSONResponse({"status": "not ready", "backend": "postgres", "error": "kafka publisher not connected"}, status_code=503)
    return {"status": "ready", "backend": "postgres"}


@router.get("/api/health")
async def api_health():
    import os
    store = await get_store()
    loop = asyncio.get_running_loop()
    data = await asyncio.to_thread(_get_cluster_health, store, loop)
    body = {
        "argo_synced": data["argo_synced"],
        "pods_running": data["pods_running"],
        "pods_failed": data["pods_failed"],
        "pipeline_status": data["pipeline_status"],
        "kafka_ready": data["kafka_ready"],
        "circuit_breakers": data.get("circuit_breakers", {}),
    }
    degraded = False
    if data["pods_failed"] > 0:
        degraded = True
    if os.environ.get("AGENTIT_KAFKA_BOOTSTRAP") and data["kafka_ready"] is False:
        degraded = True
    if data["argo_synced"] is False:
        degraded = True
    return JSONResponse(body, status_code=503 if degraded else 200)


@router.get("/api/platform/drift")
async def platform_drift():
    """Check for API drift on the cluster."""
    from agentit.platform_context import discover_platform, offline_context
    from agentit.api_drift_detector import detect_drift
    try:
        ctx = discover_platform()
    except Exception:
        ctx = offline_context()
    drift = detect_drift(ctx.available_kinds, ctx.installed_operators)
    return {
        "platform": ctx.summary(),
        "drift": {
            "removed_apis": drift.removed_apis,
            "deprecated_apis": [d.get("api", "") for d in ctx.deprecated_apis],
            "new_apis": drift.new_apis[:20],
            "has_breaking_changes": drift.has_breaking_changes,
        },
        "summary": drift.summary(),
    }


@router.get("/api/operator-status")
async def operator_status(request: Request):
    """Check if an OLM operator is installed and ready. Used by htmx polling."""
    package = request.query_params.get("package", "")
    if not package:
        return HTMLResponse("<span>Missing package name</span>")

    # `package` is client-supplied (htmx polling query param) and `phase`/`state`
    # come from cluster objects — none of these are trusted, so every value that
    # gets interpolated into the raw HTML strings below must be escaped to avoid
    # reflected XSS (this response bypasses Jinja2 autoescaping entirely).
    safe_package = html.escape(package)
    op_ns = f"openshift-{package.replace('_', '-')}"
    try:
        csvs = await asyncio.to_thread(
            kube.list_custom_resources, "operators.coreos.com", "v1alpha1", "clusterserviceversions", op_ns,
        )
        # CSV names are always "<package>.v<version>" (e.g.
        # "vertical-pod-autoscaler.v4.21.0-202606301919") -- match on that,
        # not spec.displayName, which is a human-readable string ("Vertical
        # Pod Autoscaler Operator") that never equals the OLM package name.
        # The displayName comparison always missed, verified live: the phase
        # was always "" and this endpoint reported "Waiting for operator
        # pod..." forever even after the CSV had actually reached Succeeded.
        phase = next(
            (c.get("status", {}).get("phase", "") for c in csvs
             if c.get("metadata", {}).get("name", "").startswith(f"{package}.")),
            "",
        )
        if not phase:
            sub = await asyncio.to_thread(
                _get_custom_resource_safe, "operators.coreos.com", "v1alpha1", "subscriptions", package, op_ns,
            )
            state = (sub or {}).get("status", {}).get("state", "")
            if state:
                return HTMLResponse(
                    f"<strong>{safe_package}</strong> -- subscription {html.escape(state)}. Waiting for operator pod..."
                    '<span class="spinner"></span>'
                )
            return HTMLResponse(
                f"<strong>{safe_package}</strong> -- installing..."
                '<span class="spinner"></span>'
            )
        if phase == "Succeeded":
            return HTMLResponse(
                f'<strong>{safe_package}</strong> -- <span class="badge badge-low">Installed</span>. '
                "Re-run Dry Run to apply the previously skipped manifests."
            )
        return HTMLResponse(
            f"<strong>{safe_package}</strong> -- phase: {html.escape(phase)}"
            '<span class="spinner"></span>'
        )
    except Exception:
        return HTMLResponse(f"<strong>{safe_package}</strong> -- checking..." '<span class="spinner"></span>')
