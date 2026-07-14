"""Shared agent primitives — GeneratedFile, _sanitize_name, manifest validation."""

from __future__ import annotations

import logging

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class GeneratedFile(BaseModel):
    path: str
    content: str
    description: str
    finding_addressed: str = ""
    # Set by SkillEngine.generate() to the exact skill.name that produced
    # this file -- lets callers (e.g. cli.py's self-fix) record skill
    # effectiveness precisely instead of re-deriving a skill name from the
    # file path (which is ambiguous once app_name/skill.name both contain
    # hyphens). Left "" for files a Python agent generated -- they carry no
    # skill attribution.
    skill_name: str = ""
    # Set by CodeChangeAgent to the real destination path in the *app's own*
    # repo (e.g. "Dockerfile", ".gitignore") -- distinct from `path`, which is
    # this file's name in AgentIT's own output/storage (e.g.
    # "patch-01-Dockerfile"). Lets the unified delivery router build a real
    # PR patch against the actual target file instead of a same-named copy
    # under a new directory (see docs/unified-apply-flow.md's "GitHub/
    # source-repo changes -- real source patches" taxonomy row). Left "" for
    # every other file, which fall back to `path` unchanged.
    target_path: str = ""


def _sanitize_name(name: str) -> str:
    """Turn a repo name into a k8s-safe DNS label."""
    sanitized = name.lower().replace("_", "-").replace(".", "-")[:63]
    return sanitized.strip("-") or "app"


def validate_manifest(content: str) -> list[str]:
    """Validate YAML content as K8s manifests. Returns error strings (empty = valid).

    Non-K8s YAML files (e.g. dependabot.yml, renovate.json) are detected by the
    absence of all three required K8s fields and silently skipped.
    """
    errors: list[str] = []
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    k8s_fields = {"apiVersion", "kind", "metadata"}

    for i, doc in enumerate(docs):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            errors.append(f"Document {i}: expected mapping, got {type(doc).__name__}")
            continue

        present = k8s_fields & set(doc.keys())
        if not present:
            # No K8s fields at all — not a K8s manifest, skip validation
            continue

        if "apiVersion" not in doc:
            errors.append(f"Document {i}: missing 'apiVersion'")
        if "kind" not in doc:
            errors.append(f"Document {i}: missing 'kind'")

        meta = doc.get("metadata")
        if meta is None:
            errors.append(f"Document {i}: missing 'metadata'")
        elif isinstance(meta, dict) and "name" not in meta and "generateName" not in meta:
            errors.append(f"Document {i}: metadata missing 'name' or 'generateName'")

    return errors


def make_cronjob(
    name: str,
    schedule: str,
    command: list[str],
    *,
    concurrency: str = "Forbid",
    image: str = "REPLACE_WITH_AGENTIT_IMAGE",
) -> dict:
    """Build a K8s CronJob dict (no CRD dependency)."""
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/name": name.rsplit("-", 1)[0]},
        },
        "spec": {
            "schedule": schedule,
            "concurrencyPolicy": concurrency,
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 2,
                    "activeDeadlineSeconds": 3600,
                    "template": {
                        "spec": {
                            "restartPolicy": "OnFailure",
                            "containers": [{
                                "name": "job",
                                "image": image,
                                "command": command,
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "256Mi"},
                                    "limits": {"cpu": "500m", "memory": "512Mi"},
                                },
                            }],
                        },
                    },
                },
            },
        },
    }


def validate_generated_files(files: list[GeneratedFile]) -> list[str]:
    """Validate all YAML files in a list. Returns aggregated errors."""
    all_errors: list[str] = []
    for f in files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        errors = validate_manifest(f.content)
        for e in errors:
            msg = f"{f.path}: {e}"
            all_errors.append(msg)
            logger.warning("Manifest validation: %s", msg)
    return all_errors
