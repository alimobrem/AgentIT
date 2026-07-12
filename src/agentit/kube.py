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
