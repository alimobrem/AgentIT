"""Build and push container images for onboarded apps via Tekton."""

from __future__ import annotations

import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

INTERNAL_REGISTRY = "image-registry.openshift-image-registry.svc:5000"
BUILD_TIMEOUT = 600  # 10 minutes


def get_image_ref(app_name: str, namespace: str = "agentit") -> str:
    """Return the internal registry image reference for an app."""
    name = app_name.lower().replace("_", "-").replace(".", "-")
    return f"{INTERNAL_REGISTRY}/{namespace}/{name}:latest"


def build_app_image(
    repo_url: str,
    app_name: str,
    namespace: str = "agentit",
    dockerfile: str = "Dockerfile",
) -> dict:
    """Trigger a Tekton PipelineRun to build and push the app image.

    Returns {"image_ref", "status"} or {"error"}.
    """
    name = app_name.lower().replace("_", "-").replace(".", "-")
    image_ref = get_image_ref(app_name, namespace)
    run_name = f"build-{name}-{int(time.time()) % 100000}"

    pipelinerun = {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": run_name,
            "namespace": namespace,
        },
        "spec": {
            "pipelineSpec": {
                "params": [
                    {"name": "repo-url", "type": "string"},
                    {"name": "image-ref", "type": "string"},
                    {"name": "dockerfile", "type": "string"},
                ],
                "workspaces": [{"name": "source"}],
                "tasks": [
                    {
                        "name": "git-clone",
                        "taskRef": {
                            "resolver": "cluster",
                            "params": [
                                {"name": "kind", "value": "task"},
                                {"name": "name", "value": "git-clone"},
                                {"name": "namespace", "value": "openshift-pipelines"},
                            ],
                        },
                        "params": [
                            {"name": "URL", "value": "$(params.repo-url)"},
                            {"name": "REVISION", "value": "main"},
                        ],
                        "workspaces": [{"name": "output", "workspace": "source"}],
                    },
                    {
                        "name": "build-push",
                        "runAfter": ["git-clone"],
                        "taskRef": {
                            "resolver": "cluster",
                            "params": [
                                {"name": "kind", "value": "task"},
                                {"name": "name", "value": "buildah"},
                                {"name": "namespace", "value": "openshift-pipelines"},
                            ],
                        },
                        "params": [
                            {"name": "IMAGE", "value": "$(params.image-ref)"},
                            {"name": "DOCKERFILE", "value": "$(params.dockerfile)"},
                            {"name": "CONTEXT", "value": "."},
                        ],
                        "workspaces": [{"name": "source", "workspace": "source"}],
                    },
                ],
            },
            "params": [
                {"name": "repo-url", "value": repo_url},
                {"name": "image-ref", "value": image_ref},
                {"name": "dockerfile", "value": dockerfile},
            ],
            "workspaces": [
                {
                    "name": "source",
                    "volumeClaimTemplate": {
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "resources": {"requests": {"storage": "1Gi"}},
                        },
                    },
                },
            ],
        },
    }

    try:
        result = subprocess.run(
            ["oc", "apply", "-f", "-", "-n", namespace],
            input=json.dumps(pipelinerun),
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {"error": f"Failed to create PipelineRun: {result.stderr[:200]}"}

        logger.info("Build triggered: %s for %s", run_name, app_name)
        return {
            "image_ref": image_ref,
            "run_name": run_name,
            "status": "running",
        }

    except Exception as exc:
        return {"error": str(exc)}


def wait_for_build(run_name: str, namespace: str = "agentit", timeout: int = BUILD_TIMEOUT) -> dict:
    """Wait for a PipelineRun to complete. Returns {"status": "Succeeded"|"Failed"}."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            result = subprocess.run(
                ["oc", "get", "pipelinerun", run_name, "-n", namespace,
                 "-o", "jsonpath={.status.conditions[0].reason}"],
                capture_output=True, text=True, timeout=10,
            )
            status = result.stdout.strip()
            if status == "Succeeded":
                return {"status": "Succeeded"}
            if status in ("Failed", "PipelineRunTimeout"):
                return {"status": "Failed", "reason": status}
        except Exception:
            pass
        time.sleep(15)

    return {"status": "Timeout"}
