"""Health / system status endpoints."""
from __future__ import annotations

import asyncio
import html
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agentit import kube
from agentit.portal.helpers import get_circuit_breaker_states, get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


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


def _get_cluster_health(store=None) -> dict:
    """Runs off the event loop via ``asyncio.to_thread`` at every call site,
    so ``store`` must already be a synchronous handle (``(await
    get_store()).raw``), not the async facade itself."""
    from agentit import kube

    import os
    result: dict = {
        "argo_apps": [], "argo_synced": False,
        "pods": [], "pods_running": 0, "pods_failed": 0,
        "pipelines": [], "pipeline_status": "Unknown",
        "kafka_ready": False, "publisher_ok": False,
        "namespace": os.environ.get("AGENTIT_NAMESPACE", "agentit"),
        "cluster_url": os.environ.get("KUBERNETES_SERVICE_HOST", "local"),
    }

    managed_names = {"agentit"}
    try:
        if store is not None:
            for app_data in store.get_fleet_data():
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
        for r in reversed(all_runs):
            conds = r.get("status", {}).get("conditions", [{}])
            if conds and conds[0].get("reason") == "Succeeded":
                ct = r.get("status", {}).get("completionTime", "")
                result["last_successful_ci"] = ct[:19] if ct else "?"
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
            result["current_commit"] = rev[:12]
        else:
            result["current_commit"] = ""
    except Exception:
        log.debug("Failed to get current commit from Argo CD", exc_info=True)
        result["current_commit"] = ""

    # Kafka
    try:
        kafkas = kube.list_custom_resources("kafka.strimzi.io", "v1beta2", "kafkas", namespace="agentit")
        if kafkas:
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

    return result


# ── Routes ────────────────────────────────────────────────────────────


async def _sync_store_handle():
    """Resolve the async store singleton, returning its synchronous handle
    for use inside ``asyncio.to_thread`` workers (see module docstring on
    ``_get_cluster_health``). Returns ``None`` for the postgres backend
    (no ``.raw`` yet) rather than guessing."""
    s = await get_store()
    return s.raw if hasattr(s, "raw") else None


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> HTMLResponse:
    store = await _sync_store_handle()
    data = await asyncio.to_thread(_get_cluster_health, store)
    return get_templates().TemplateResponse(request, "health.html", data)


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
        if hasattr(s, "get_setting"):
            await s.get_setting("__healthz_probe__")
        else:
            s._conn.execute("SELECT 1")
    except Exception as exc:
        return JSONResponse({"status": "unhealthy", "error": str(exc)}, status_code=503)
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    import os
    try:
        s = await get_store()
        if hasattr(s, "get_setting"):
            await s.get_setting("__readyz_probe__")
        else:
            s._conn.execute("SELECT 1")
    except Exception as exc:
        return JSONResponse({"status": "not ready", "error": str(exc)}, status_code=503)
    if os.environ.get("AGENTIT_KAFKA_BOOTSTRAP"):
        from agentit.events import get_publisher
        pub = get_publisher()
        if not pub.kafka_enabled:
            return JSONResponse({"status": "not ready", "error": "kafka publisher not connected"}, status_code=503)
    return {"status": "ready"}


@router.get("/api/health")
async def api_health():
    import os
    store = await _sync_store_handle()
    data = await asyncio.to_thread(_get_cluster_health, store)
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
            "deprecated_apis": [d.get("api", "") for d in drift.deprecated_apis] if hasattr(drift, 'deprecated_apis') and isinstance(drift.deprecated_apis, list) and drift.deprecated_apis and isinstance(drift.deprecated_apis[0], dict) else drift.deprecated_apis,
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
