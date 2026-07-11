"""Build and push container images for onboarded apps via Tekton."""

from __future__ import annotations

import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

INTERNAL_REGISTRY = "image-registry.openshift-image-registry.svc:5000"
BUILD_TIMEOUT = 600  # 10 minutes


def _generate_dockerfile_script(app_name: str) -> str:
    """Generate a shell script that creates a Dockerfile if none exists."""
    return f"""\
#!/bin/sh
set -e
cd $(workspaces.source.path)
if [ -f Dockerfile ] || [ -f Containerfile ]; then
  echo "Dockerfile found — using existing"
  # Ensure the DOCKERFILE param matches what exists
  if [ -f Containerfile ] && [ ! -f Dockerfile ]; then
    cp Containerfile Dockerfile
  fi
  exit 0
fi
echo "No Dockerfile found — auto-generating for {app_name}"
# Detect language
if [ -f go.mod ]; then
  cat > Dockerfile <<'GOEOF'
FROM registry.access.redhat.com/ubi9/go-toolset:latest AS builder
WORKDIR /app
COPY . .
RUN go build -o app .
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
COPY --from=builder /app/app /usr/local/bin/app
USER 1001
EXPOSE 8080
CMD ["app"]
GOEOF
elif [ -f package.json ]; then
  cat > Dockerfile <<'NODEEOF'
FROM registry.access.redhat.com/ubi9/nodejs-20:latest
WORKDIR /app
COPY package*.json ./
RUN npm ci --production
COPY . .
USER 1001
EXPOSE 3000
CMD ["node", "index.js"]
NODEEOF
elif [ -f requirements.txt ] || [ -f pyproject.toml ]; then
  cat > Dockerfile <<'PYEOF'
FROM registry.access.redhat.com/ubi9/python-312:latest
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir . 2>/dev/null || true
USER 1001
EXPOSE 8080
CMD ["python", "app.py"]
PYEOF
elif [ -f pom.xml ]; then
  cat > Dockerfile <<'JAVAEOF'
FROM registry.access.redhat.com/ubi9/openjdk-21:latest
WORKDIR /app
COPY . .
RUN mvn package -DskipTests 2>/dev/null || true
USER 1001
EXPOSE 8080
CMD ["java", "-jar", "target/*.jar"]
JAVAEOF
else
  cat > Dockerfile <<'DEFAULTEOF'
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
WORKDIR /app
COPY . .
USER 1001
EXPOSE 8080
DEFAULTEOF
fi
echo "Generated Dockerfile for {app_name}"
cat Dockerfile
"""


def get_image_ref(app_name: str, namespace: str = "agentit") -> str:
    """Return the internal registry image reference for an app."""
    name = app_name.lower().replace("_", "-").replace(".", "-")
    return f"{INTERNAL_REGISTRY}/{namespace}/{name}:latest"


def build_app_image(
    repo_url: str,
    app_name: str,
    namespace: str = "agentit",
    dockerfile: str = "Dockerfile",
    containerfile_content: str | None = None,
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
                        "name": "ensure-dockerfile",
                        "runAfter": ["git-clone"],
                        "taskSpec": {
                            "workspaces": [{"name": "source"}],
                            "steps": [{
                                "name": "check-or-create",
                                "image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
                                "script": _generate_dockerfile_script(app_name),
                            }],
                        },
                        "workspaces": [{"name": "source", "workspace": "source"}],
                    },
                    {
                        "name": "build-push",
                        "runAfter": ["ensure-dockerfile"],
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
