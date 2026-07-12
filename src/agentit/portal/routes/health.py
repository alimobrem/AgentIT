"""Health / system status endpoints."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agentit.portal.helpers import get_store, get_templates

log = logging.getLogger(__name__)

router = APIRouter()


# ── Internal helpers ──────────────────────────────────────────────────


def _run_cmd(cmd: list[str], timeout: int = 10) -> str | None:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _get_cluster_health() -> dict:
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
        _s = get_store()
        for app_data in _s.get_fleet_data():
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
        result["argo_synced"] = all(a["sync"] == "Synced" for a in result["argo_apps"]) if result["argo_apps"] else True
    except Exception:
        log.warning("Failed to fetch Argo CD apps", exc_info=True)

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

    return result


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> HTMLResponse:
    data = await asyncio.to_thread(_get_cluster_health)
    return get_templates().TemplateResponse(request, "health.html", data)


@router.get("/health/pods/{pod_name}", response_class=HTMLResponse)
async def pod_detail_page(request: Request, pod_name: str) -> HTMLResponse:
    import json as _json

    raw = await asyncio.to_thread(_run_cmd, ["oc", "get", "pod", pod_name, "-n", "agentit", "-o", "json"])
    if not raw:
        raise HTTPException(404, f"Pod {pod_name} not found")

    pod = _json.loads(raw)
    status = pod.get("status", {}).get("phase", "Unknown")
    created = pod.get("metadata", {}).get("creationTimestamp", "")[:19]
    containers = []
    total_restarts = 0
    for cs in pod.get("status", {}).get("containerStatuses", []):
        restarts = cs.get("restartCount", 0)
        total_restarts += restarts
        containers.append({
            "name": cs.get("name", "?"),
            "image": cs.get("image", "?"),
            "ready": cs.get("ready", False),
            "restarts": restarts,
        })

    logs = await asyncio.to_thread(
        _run_cmd, ["oc", "logs", pod_name, "-n", "agentit", "--tail=100"], 15,
    ) or "No logs available"

    events_raw = await asyncio.to_thread(
        _run_cmd, ["oc", "get", "events", "-n", "agentit", "--field-selector",
                   f"involvedObject.name={pod_name}", "-o", "json"],
    )
    events = []
    if events_raw:
        try:
            for e in _json.loads(events_raw).get("items", []):
                events.append({
                    "time": e.get("lastTimestamp", "")[:19],
                    "type": e.get("type", "Normal"),
                    "reason": e.get("reason", "?"),
                    "message": e.get("message", "")[:200],
                })
        except Exception:
            log.debug("Failed to parse pod events", exc_info=True)

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
    import json as _json

    raw = await asyncio.to_thread(
        _run_cmd, ["oc", "get", "pipelinerun", pipeline_name, "-n", "agentit", "-o", "json"],
    )
    if not raw:
        raise HTTPException(404, f"PipelineRun {pipeline_name} not found")

    pr = _json.loads(raw)
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
            logs = await asyncio.to_thread(
                _run_cmd, ["oc", "logs", last_pod, "-n", "agentit", "--tail=50", "--all-containers"], 15,
            ) or ""

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
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    try:
        s = get_store()
        s._conn.execute("SELECT 1")
        return {"status": "ready"}
    except Exception as exc:
        return JSONResponse({"status": "not ready", "error": str(exc)}, status_code=503)


@router.get("/api/health")
async def api_health():
    data = await asyncio.to_thread(_get_cluster_health)
    return JSONResponse({
        "argo_synced": data["argo_synced"],
        "pods_running": data["pods_running"],
        "pipeline_status": data["pipeline_status"],
        "kafka_ready": data["kafka_ready"],
    })


@router.get("/api/operator-status")
async def operator_status(request: Request):
    """Check if an OLM operator is installed and ready. Used by htmx polling."""
    import shutil
    import subprocess

    package = request.query_params.get("package", "")
    if not package:
        return HTMLResponse("<span>Missing package name</span>")

    cli = shutil.which("oc") or shutil.which("kubectl")
    if not cli:
        return HTMLResponse(f"<strong>{package}</strong> -- cannot check (no oc/kubectl)")

    op_ns = f"openshift-{package.replace('_', '-')}"
    try:
        result = subprocess.run(
            [cli, "get", "csv", "-n", op_ns, "-o",
             f"jsonpath={{.items[?(@.spec.displayName=='{package}')].status.phase}}"],
            capture_output=True, text=True, timeout=10,
        )
        phase = result.stdout.strip()
        if not phase:
            result2 = subprocess.run(
                [cli, "get", "subscription.operators.coreos.com", package, "-n", op_ns,
                 "-o", "jsonpath={.status.state}"],
                capture_output=True, text=True, timeout=10,
            )
            state = result2.stdout.strip()
            if state:
                return HTMLResponse(
                    f"<strong>{package}</strong> -- subscription {state}. Waiting for operator pod..."
                    '<span class="spinner"></span>'
                )
            return HTMLResponse(
                f"<strong>{package}</strong> -- installing..."
                '<span class="spinner"></span>'
            )
        if phase == "Succeeded":
            return HTMLResponse(
                f'<strong>{package}</strong> -- <span class="badge badge-low">Installed</span>. '
                "Re-run Dry Run to apply the previously skipped manifests."
            )
        return HTMLResponse(
            f"<strong>{package}</strong> -- phase: {phase}"
            '<span class="spinner"></span>'
        )
    except Exception:
        return HTMLResponse(f"<strong>{package}</strong> -- checking..." '<span class="spinner"></span>')
