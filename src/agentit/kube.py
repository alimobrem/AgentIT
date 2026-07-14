from __future__ import annotations

import logging
import time as _time

logger = logging.getLogger(__name__)


class KubeError(Exception):
    """Raised when a Kubernetes API call fails (not a missing-resource 404)."""

_client_cache = None
_client_cache_time: float = 0
_CLIENT_TTL = 600  # 10 minutes


def get_client():
    """Get a configured kubernetes client. Auto-detects in-cluster vs kubeconfig.

    Cached with a 10-minute TTL so bound service-account tokens (which rotate
    hourly) are picked up after expiry.
    """
    global _client_cache, _client_cache_time
    now = _time.monotonic()
    if _client_cache is not None and (now - _client_cache_time) < _CLIENT_TTL:
        return _client_cache
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _client_cache = client
    _client_cache_time = now
    return client


_dynamic_client_cache = None
_dynamic_client_cache_time: float = 0


def dynamic_client():
    """Get a cached kubernetes dynamic client (generic apiVersion/kind ->
    REST-path resolution + server-side-apply support), used by
    ``apply_yaml()`` below. Shares ``get_client()``'s 10-minute TTL so a
    refreshed in-cluster service-account token is picked up the same way.
    """
    global _dynamic_client_cache, _dynamic_client_cache_time
    now = _time.monotonic()
    if _dynamic_client_cache is not None and (now - _dynamic_client_cache_time) < _CLIENT_TTL:
        return _dynamic_client_cache
    from kubernetes import dynamic

    api_client = get_client().ApiClient()
    _dynamic_client_cache = dynamic.DynamicClient(api_client)
    _dynamic_client_cache_time = now
    return _dynamic_client_cache


def core_v1():
    return get_client().CoreV1Api()


def apps_v1():
    return get_client().AppsV1Api()


def custom_objects():
    return get_client().CustomObjectsApi()


def batch_v1():
    return get_client().BatchV1Api()


def list_pods(namespace: str, label_selector: str = "") -> list[dict]:
    """List pods in a namespace, returns simplified dicts."""
    try:
        pods = core_v1().list_namespaced_pod(namespace, label_selector=label_selector, _request_timeout=10)
        return [
            {
                "name": p.metadata.name,
                "status": p.status.phase,
                "restarts": sum(cs.restart_count for cs in (p.status.container_statuses or [])),
                "age": p.metadata.creation_timestamp.isoformat()[:16] if p.metadata.creation_timestamp else "",
                "ready": all(cs.ready for cs in (p.status.container_statuses or [])),
                "crash_looping": any(
                    cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff"
                    for cs in (p.status.container_statuses or [])
                ),
                "container_statuses": p.status.container_statuses or [],
            }
            for p in pods.items
        ]
    except Exception as exc:
        raise KubeError(f"Failed to list pods in {namespace}: {exc}") from exc


def get_pod_count(namespace: str) -> tuple[int, int]:
    """Returns (running_count, failed_count). Raises KubeError on API failure."""
    pods = list_pods(namespace)
    running = sum(1 for p in pods if p["status"] == "Running")
    failed = sum(1 for p in pods if p["status"] == "Failed" or p.get("crash_looping", False))
    return running, failed


def list_custom_resources(group: str, version: str, plural: str, namespace: str = "") -> list[dict]:
    """List custom resources. Returns raw dicts."""
    try:
        if namespace:
            result = custom_objects().list_namespaced_custom_object(group, version, namespace, plural, _request_timeout=10)
        else:
            result = custom_objects().list_cluster_custom_object(group, version, plural, _request_timeout=10)
        return result.get("items", [])
    except Exception as exc:
        raise KubeError(f"Failed to list {group}/{version} {plural}: {exc}") from exc


def get_custom_resource(group: str, version: str, plural: str, name: str, namespace: str = "") -> dict | None:
    """Get a single custom resource by name. Returns None if not found (404)."""
    try:
        if namespace:
            return custom_objects().get_namespaced_custom_object(group, version, namespace, plural, name, _request_timeout=10)
        return custom_objects().get_cluster_custom_object(group, version, plural, name, _request_timeout=10)
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 404:
            return None
        raise KubeError(f"Failed to get {group}/{version} {plural}/{name}: {exc}") from exc


def create_custom_resource(group: str, version: str, plural: str, namespace: str, body: dict) -> dict:
    """Create a namespaced custom resource. Raises KubeError on failure (including 'already exists')."""
    try:
        return custom_objects().create_namespaced_custom_object(group, version, namespace, plural, body, _request_timeout=15)
    except Exception as exc:
        raise KubeError(f"Failed to create {group}/{version} {plural}: {exc}") from exc


def patch_custom_resource(group: str, version: str, plural: str, name: str, namespace: str, body: dict) -> dict:
    """Merge-patch an existing namespaced custom resource. Raises KubeError on failure."""
    try:
        return custom_objects().patch_namespaced_custom_object(group, version, namespace, plural, name, body, _request_timeout=15)
    except Exception as exc:
        raise KubeError(f"Failed to patch {group}/{version} {plural}/{name}: {exc}") from exc


DEFAULT_FIELD_MANAGER = "agentit"


def _ssa_conflict_message(exc) -> str:
    """Best-effort human-readable message from a 409 server-side-apply
    conflict response. The apiserver's error body is JSON with a `message`
    field naming the conflicting field manager(s) and path(s)
    (e.g. `Apply failed with 1 conflict: conflict with "kubectl-client-side-apply"
    using v1: .data.foo`) -- fall back to `str(exc)` if the body isn't the
    shape we expect (defensive against apiserver version differences)."""
    import json

    try:
        body = json.loads(exc.body) if exc.body else {}
        return body.get("message") or str(exc)
    except (TypeError, ValueError, AttributeError):
        return str(exc)


def apply_yaml(
    content: str, namespace: str, *,
    field_manager: str = DEFAULT_FIELD_MANAGER, force: bool = False,
) -> dict:
    """Apply a YAML manifest via real per-field-manager server-side-apply.

    `content` is an arbitrary, multi-document manifest spanning both
    core/typed kinds (ConfigMap, Namespace, ...) and CRDs (Application,
    Rollout, Subscription, ...) -- each document is resolved to its REST
    resource via the kubernetes client's dynamic-client discovery, then
    applied with `content_type="application/apply-patch+yaml"` and
    `field_manager=field_manager`, matching `oc apply --server-side`'s wire
    format but through the real Python client instead of an `oc` subprocess.

    `force` defaults to `False` and is passed through as SSA's `force`
    query parameter (`force_conflicts` in the dynamic client) only when the
    caller explicitly opts in -- on a genuine field-manager conflict (HTTP
    409, `reason=Conflict`) this does NOT silently retry with force; it
    returns a structured, distinguishable result instead so callers can
    route the conflict to a human-reviewed gate rather than either
    failing silently or seizing ownership from another manager.

    Returns a dict:
      - applied: True only if every document in `content` applied cleanly.
      - error: a short message (first failure) when `applied` is False.
      - conflict: True when the failure was purely field-manager conflict(s)
        (never true when `applied` is True, and never true alongside a
        non-conflict failure -- a hard error takes precedence for `error`/
        `conflict` since it needs attention regardless of ownership).
      - conflict_details: per-document conflict info (kind/name/namespace/
        apiserver message), empty unless `conflict` is True. One `content`
        call can contain multiple documents (see above), so this is a list,
        not a single conflict -- matching this function's existing
        per-*file* (not per-document) result granularity used throughout
        `cluster_apply.py`.
    """
    import yaml as _yaml
    from kubernetes.client.exceptions import ApiException

    try:
        docs = [d for d in _yaml.safe_load_all(content) if isinstance(d, dict)]
    except _yaml.YAMLError as exc:
        return {"applied": False, "error": f"invalid YAML: {exc}", "conflict": False, "conflict_details": []}

    if not docs:
        return {"applied": True, "error": None, "conflict": False, "conflict_details": []}

    dyn = dynamic_client()
    conflict_details: list[dict] = []
    first_error: str | None = None
    any_conflict = False
    any_failure = False

    for doc in docs:
        kind = doc.get("kind", "")
        api_version = doc.get("apiVersion", "")
        meta = doc.get("metadata") or {}
        name = meta.get("name", "")
        doc_namespace = meta.get("namespace") or namespace

        if not kind or not api_version or not name:
            any_failure = True
            first_error = first_error or f"document missing kind/apiVersion/name (kind={kind!r}, name={name!r})"
            continue

        try:
            resource = dyn.resources.get(api_version=api_version, kind=kind)
        except Exception as exc:
            any_failure = True
            first_error = first_error or f"{kind} ({api_version}) not found on cluster: {exc}"
            continue

        try:
            dyn.server_side_apply(
                resource, body=doc, name=name,
                namespace=doc_namespace if resource.namespaced else None,
                field_manager=field_manager, force_conflicts=force,
                _request_timeout=30,
            )
        except ApiException as exc:
            if exc.status == 409:
                any_conflict = True
                message = _ssa_conflict_message(exc)
                conflict_details.append({
                    "kind": kind, "name": name, "namespace": doc_namespace, "message": message,
                })
                first_error = first_error or f"{kind}/{name}: field-manager conflict -- {message}"
            else:
                any_failure = True
                first_error = first_error or f"{kind}/{name}: {exc.reason or exc.body or exc}"
        except Exception as exc:
            any_failure = True
            first_error = first_error or f"{kind}/{name}: {exc}"

    if not any_failure and not any_conflict:
        return {"applied": True, "error": None, "conflict": False, "conflict_details": []}

    return {
        "applied": False,
        "error": first_error,
        "conflict": any_conflict and not any_failure,
        "conflict_details": conflict_details if (any_conflict and not any_failure) else [],
    }


def _get_argo_rollout(name: str, namespace: str) -> dict | None:
    """Return the Argo Rollout custom resource with this name, or None if it
    doesn't exist (or the Rollout CRD isn't installed on this cluster)."""
    try:
        return custom_objects().get_namespaced_custom_object(
            "argoproj.io", "v1alpha1", namespace, "rollouts", name, _request_timeout=10,
        )
    except Exception:
        return None


def rollout_undo(deployment: str, namespace: str) -> dict:
    """Roll back a deployment to its previous stable state.

    For Argo Rollouts-managed apps (kind ``Rollout``, group ``argoproj.io``),
    this patches the Rollout's ``status`` subresource with ``{"abort": true}``
    -- the same mechanism ``kubectl argo rollouts abort`` uses -- which halts
    the canary and reverts traffic to the previous stable ReplicaSet.

    For plain Deployments (no matching Rollout found), apps/v1 does not
    support ``spec.rollbackTo`` (that was extensions/v1beta1), so this falls
    back to a restart via a pod-template annotation, equivalent to
    ``kubectl rollout restart``. That is NOT a true rollback -- it re-deploys
    the current spec rather than reverting to a previous ReplicaSet -- but is
    the best available fallback for non-Rollout resources.
    """
    if _get_argo_rollout(deployment, namespace) is not None:
        try:
            custom_objects().patch_namespaced_custom_object_status(
                "argoproj.io", "v1alpha1", namespace, "rollouts", deployment,
                body={"status": {"abort": True}}, _request_timeout=15,
            )
            return {
                "success": True,
                "message": f"Argo Rollout '{deployment}' aborted -- reverted to previous stable ReplicaSet",
            }
        except Exception as exc:
            logger.warning("Rollout abort failed for %s/%s: %s", namespace, deployment, exc)
            return {"success": False, "message": str(exc)}

    from datetime import datetime, timezone

    try:
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat(),
                        }
                    }
                }
            }
        }
        apps_v1().patch_namespaced_deployment(deployment, namespace, body, _request_timeout=15)
        return {
            "success": True,
            "message": f"Rollout restart initiated for {deployment} "
                        "(no Argo Rollout found -- restarted the Deployment instead of a true rollback)",
        }
    except Exception as exc:
        logger.warning("Rollout restart failed for %s/%s: %s", namespace, deployment, exc)
        return {"success": False, "message": str(exc)}


def get_api_resources() -> set[str]:
    """Get available API resource kinds across ALL API groups on the cluster.

    Queries the core v1 group (Pods, Services, ConfigMaps, ...) plus every
    named API group (apps, networking.k8s.io, autoscaling, batch, policy,
    ...) via the discovery API, so ``PlatformContext.has_api()`` correctly
    reports availability for Deployments, Ingresses, HPAs, NetworkPolicies,
    etc. -- not just core resources.
    """
    import json

    try:
        resources = set()
        api_client = get_client().ApiClient()

        for api in core_v1().get_api_resources(_request_timeout=10).resources:
            resources.add(api.kind.lower())

        groups = get_client().ApisApi(api_client).get_api_versions(_request_timeout=10)
        for group in groups.groups:
            version = None
            if group.preferred_version is not None:
                version = group.preferred_version.version
            elif group.versions:
                version = group.versions[0].version
            if not version:
                continue
            try:
                resp = api_client.call_api(
                    f"/apis/{group.name}/{version}", "GET",
                    _return_http_data_only=True, _preload_content=False,
                    _request_timeout=10,
                )
                data = json.loads(resp.read())
                for res in data.get("resources", []):
                    kind = res.get("kind")
                    if kind:
                        resources.add(kind.lower())
            except Exception as exc:
                logger.debug("Failed to list resources for group %s/%s: %s", group.name, version, exc)

        return resources
    except Exception as exc:
        raise KubeError(f"Failed to get API resources: {exc}") from exc


def create_config_map(name: str, namespace: str, data: dict[str, str]) -> bool:
    """Create a ConfigMap. Returns True on success."""
    from kubernetes.client import V1ConfigMap, V1ObjectMeta
    try:
        cm = V1ConfigMap(
            metadata=V1ObjectMeta(name=name, namespace=namespace),
            data=data,
        )
        core_v1().create_namespaced_config_map(namespace, cm)
        return True
    except Exception as exc:
        if "already exists" in str(exc).lower():
            core_v1().replace_namespaced_config_map(name, namespace, V1ConfigMap(
                metadata=V1ObjectMeta(name=name, namespace=namespace),
                data=data,
            ))
            return True
        logger.warning("Failed to create ConfigMap %s: %s", name, exc)
        return False


def delete_config_map(name: str, namespace: str) -> None:
    try:
        core_v1().delete_namespaced_config_map(name, namespace)
    except Exception:
        logger.debug("delete_config_map %s/%s failed", namespace, name, exc_info=True)


def get_current_pod_image() -> str | None:
    """Auto-detect the image of the current pod (when running in-cluster)."""
    import os
    hostname = os.environ.get("HOSTNAME", "")
    if not hostname:
        return None
    namespace = os.environ.get("AGENTIT_NAMESPACE", "agentit")
    try:
        pod = core_v1().read_namespaced_pod(hostname, namespace)
        if pod.spec.containers:
            return pod.spec.containers[0].image
    except Exception as exc:
        logger.warning("Failed to auto-detect pod image: %s", exc)
    return None


def create_job(
    name: str,
    namespace: str,
    image: str,
    command: list[str],
    config_map_name: str | None = None,
    config_map_mount: str = "/input",
    active_deadline: int = 300,
    backoff_limit: int = 1,
    labels: dict[str, str] | None = None,
    resources: dict[str, str] | None = None,
) -> bool:
    """Create a K8s Job. Returns True on success."""
    from kubernetes.client import (
        V1Job, V1JobSpec, V1ObjectMeta, V1PodTemplateSpec, V1PodSpec,
        V1Container, V1ResourceRequirements, V1SecurityContext,
        V1Volume, V1VolumeMount, V1ConfigMapVolumeSource,
        V1EnvVar,
    )

    volumes = []
    volume_mounts = []
    if config_map_name:
        volumes.append(V1Volume(
            name="input",
            config_map=V1ConfigMapVolumeSource(name=config_map_name),
        ))
        volume_mounts.append(V1VolumeMount(
            name="input", mount_path=config_map_mount, read_only=True,
        ))

    all_labels = {"app.kubernetes.io/component": "agent", "agentit/job": name}
    if labels:
        all_labels.update(labels)

    res = resources or {}
    cpu_req = res.get("cpu_req", "100m")
    cpu_lim = res.get("cpu_lim", "500m")
    mem_req = res.get("mem_req", "256Mi")
    mem_lim = res.get("mem_lim", "512Mi")

    job = V1Job(
        metadata=V1ObjectMeta(name=name, namespace=namespace, labels=all_labels),
        spec=V1JobSpec(
            active_deadline_seconds=active_deadline,
            backoff_limit=backoff_limit,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=all_labels),
                spec=V1PodSpec(
                    restart_policy="Never",
                    containers=[V1Container(
                        name="agent",
                        image=image,
                        command=command,
                        volume_mounts=volume_mounts or None,
                        resources=V1ResourceRequirements(
                            requests={"cpu": cpu_req, "memory": mem_req},
                            limits={"cpu": cpu_lim, "memory": mem_lim},
                        ),
                        security_context=V1SecurityContext(
                            allow_privilege_escalation=False,
                            run_as_non_root=True,
                        ),
                        env=[V1EnvVar(name="PYTHONUNBUFFERED", value="1")],
                    )],
                    volumes=volumes or None,
                ),
            ),
        ),
    )

    try:
        batch_v1().create_namespaced_job(namespace, job)
        return True
    except Exception as exc:
        logger.warning("Failed to create Job %s: %s", name, exc)
        return False


def get_job_status(name: str, namespace: str) -> str:
    """Get Job status: 'active', 'succeeded', 'failed', or 'unknown'."""
    try:
        job = batch_v1().read_namespaced_job_status(name, namespace, _request_timeout=10)
        if job.status.succeeded and job.status.succeeded > 0:
            return "succeeded"
        if job.status.failed and job.status.failed > 0:
            return "failed"
        if job.status.active and job.status.active > 0:
            return "active"
        return "unknown"
    except Exception as exc:
        logger.warning("Failed to get Job status %s: %s", name, exc)
        return "unknown"


def get_job_pod_log(job_name: str, namespace: str) -> str:
    """Read logs from the pod created by a Job."""
    try:
        pods = core_v1().list_namespaced_pod(
            namespace, label_selector=f"agentit/job={job_name}", _request_timeout=10,
        )
        if not pods.items:
            return ""
        pod_name = pods.items[0].metadata.name
        return core_v1().read_namespaced_pod_log(pod_name, namespace, _request_timeout=30)
    except Exception as exc:
        logger.warning("Failed to read Job pod log %s: %s", job_name, exc)
        return ""


def delete_job(name: str, namespace: str) -> None:
    """Delete a Job and its pods."""
    from kubernetes.client import V1DeleteOptions
    try:
        batch_v1().delete_namespaced_job(
            name, namespace,
            body=V1DeleteOptions(propagation_policy="Background"),
        )
    except Exception:
        logger.debug("delete_job %s/%s failed", namespace, name, exc_info=True)


def namespace_exists(namespace: str) -> bool:
    try:
        core_v1().read_namespace(namespace, _request_timeout=5)
        return True
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 404:
            return False
        raise KubeError(f"Failed to check namespace {namespace}: {exc}") from exc


def create_namespace(namespace: str) -> None:
    from kubernetes.client import V1Namespace, V1ObjectMeta

    try:
        core_v1().create_namespace(V1Namespace(metadata=V1ObjectMeta(name=namespace)), _request_timeout=10)
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 409:
            return
        raise KubeError(f"Failed to create namespace {namespace}: {exc}") from exc
