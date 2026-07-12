from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


def get_client():
    """Get a configured kubernetes client. Auto-detects in-cluster vs kubeconfig."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client


@lru_cache(maxsize=1)
def core_v1():
    return get_client().CoreV1Api()


@lru_cache(maxsize=1)
def apps_v1():
    return get_client().AppsV1Api()


@lru_cache(maxsize=1)
def custom_objects():
    return get_client().CustomObjectsApi()


@lru_cache(maxsize=1)
def batch_v1():
    return get_client().BatchV1Api()


def list_pods(namespace: str, label_selector: str = "") -> list[dict]:
    """List pods in a namespace, returns simplified dicts."""
    try:
        pods = core_v1().list_namespaced_pod(namespace, label_selector=label_selector)
        return [
            {
                "name": p.metadata.name,
                "status": p.status.phase,
                "restarts": sum(cs.restart_count for cs in (p.status.container_statuses or [])),
                "age": p.metadata.creation_timestamp.isoformat()[:16] if p.metadata.creation_timestamp else "",
                "ready": all(cs.ready for cs in (p.status.container_statuses or [])),
                "container_statuses": p.status.container_statuses or [],
            }
            for p in pods.items
        ]
    except Exception as exc:
        logger.warning("Failed to list pods in %s: %s", namespace, exc)
        return []


def get_pod_count(namespace: str) -> tuple[int, int]:
    """Returns (running_count, failed_count)."""
    pods = list_pods(namespace)
    running = sum(1 for p in pods if p["status"] == "Running")
    failed = sum(1 for p in pods if p["status"] in ("Failed", "Error", "CrashLoopBackOff"))
    return running, failed


def list_custom_resources(group: str, version: str, plural: str, namespace: str = "") -> list[dict]:
    """List custom resources. Returns raw dicts."""
    try:
        if namespace:
            result = custom_objects().list_namespaced_custom_object(group, version, namespace, plural)
        else:
            result = custom_objects().list_cluster_custom_object(group, version, plural)
        return result.get("items", [])
    except Exception as exc:
        logger.warning("Failed to list %s/%s %s: %s", group, version, plural, exc)
        return []


def apply_yaml(content: str, namespace: str, dry_run: bool = False) -> dict:
    """Apply a YAML manifest using create_from_yaml. Returns {"applied": bool, "error": str|None}."""
    import os
    import tempfile

    import yaml
    from kubernetes import client as k8s_client
    from kubernetes.utils import create_from_yaml

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    try:
        tmp.write(content)
        tmp.close()
        api_client = k8s_client.ApiClient()
        try:
            create_from_yaml(
                api_client,
                tmp.name,
                namespace=namespace,
                verbose=False,
            )
            return {"applied": True, "error": None}
        except Exception as exc:
            error_msg = str(exc)
            if "already exists" in error_msg.lower():
                return {"applied": True, "error": None}
            return {"applied": False, "error": error_msg[:200]}
    finally:
        os.unlink(tmp.name)


def rollout_undo(deployment: str, namespace: str) -> dict:
    """Rollback a deployment. Returns {"success": bool, "message": str}."""
    try:
        body = {"spec": {"rollbackTo": {"revision": 0}}}
        apps_v1().patch_namespaced_deployment(deployment, namespace, body)
        return {"success": True, "message": f"Rollback initiated for {deployment}"}
    except Exception as exc:
        logger.warning("Rollback failed for %s/%s: %s", namespace, deployment, exc)
        return {"success": False, "message": str(exc)}


def get_api_resources() -> set[str]:
    """Get available API resource kinds on the cluster."""
    try:
        resources = set()
        for api in core_v1().get_api_resources().resources:
            resources.add(api.kind.lower())
        return resources
    except Exception as exc:
        logger.warning("Failed to get API resources: %s", exc)
        return set()


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
        pass


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
        job = batch_v1().read_namespaced_job_status(name, namespace)
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
            namespace, label_selector=f"agentit/job={job_name}",
        )
        if not pods.items:
            return ""
        pod_name = pods.items[0].metadata.name
        return core_v1().read_namespaced_pod_log(pod_name, namespace)
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
        pass


def namespace_exists(namespace: str) -> bool:
    try:
        core_v1().read_namespace(namespace)
        return True
    except Exception:
        return False


def create_namespace(namespace: str) -> None:
    from kubernetes.client import V1Namespace, V1ObjectMeta

    try:
        core_v1().create_namespace(V1Namespace(metadata=V1ObjectMeta(name=namespace)))
    except Exception as exc:
        logger.warning("Failed to create namespace %s: %s", namespace, exc)
