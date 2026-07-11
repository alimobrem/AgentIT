"""Shared agent primitives — GeneratedFile, _sanitize_name, manifest validation."""

from __future__ import annotations

import logging

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)

KNOWN_API_GROUPS = {
    "v1",
    "apps/v1",
    "batch/v1",
    "networking.k8s.io/v1",
    "rbac.authorization.k8s.io/v1",
    "policy/v1",
    "autoscaling/v1",
    "autoscaling/v2",
    "autoscaling.k8s.io/v1",
    "monitoring.coreos.com/v1",
    "argoproj.io/v1alpha1",
    "tekton.dev/v1beta1",
    "tekton.dev/v1",
    "triggers.tekton.dev/v1beta1",
    "triggers.tekton.dev/v1alpha1",
    "kyverno.io/v1",
    "litmuschaos.io/v1alpha1",
    "opentelemetry.io/v1alpha1",
    "integreatly.org/v1alpha1",
    "argoproj.io/v1",
    "route.openshift.io/v1",
}


class GeneratedFile(BaseModel):
    path: str
    content: str
    description: str
    finding_addressed: str = ""


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
