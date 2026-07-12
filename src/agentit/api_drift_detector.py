"""API drift detector — tracks cluster API surface changes between runs."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = Path(os.environ.get("AGENTIT_API_SNAPSHOT", ".agentit-api-snapshot.json"))


@dataclass
class DriftResult:
    """Result of comparing current API surface to the last snapshot."""

    new_apis: list[str] = field(default_factory=list)
    removed_apis: list[str] = field(default_factory=list)
    new_operators: list[str] = field(default_factory=list)
    removed_operators: list[str] = field(default_factory=list)

    @property
    def has_breaking_changes(self) -> bool:
        return len(self.removed_apis) > 0 or len(self.removed_operators) > 0

    def summary(self) -> str:
        parts: list[str] = []
        if self.new_apis:
            parts.append(f"+{len(self.new_apis)} new APIs")
        if self.removed_apis:
            parts.append(f"-{len(self.removed_apis)} removed APIs (BREAKING)")
        if self.new_operators:
            parts.append(f"+{len(self.new_operators)} new operators")
        if self.removed_operators:
            parts.append(f"-{len(self.removed_operators)} removed operators")
        return ", ".join(parts) if parts else "No drift detected"


def _snapshot_path() -> Path:
    """Resolve the snapshot file path, respecting env override."""
    override = os.environ.get("AGENTIT_API_SNAPSHOT")
    if override:
        return Path(override)
    return SNAPSHOT_PATH


def save_snapshot(kinds: set[str], operators: list[str]) -> None:
    """Persist the current API surface to disk."""
    path = _snapshot_path()
    data = {"kinds": sorted(kinds), "operators": sorted(operators)}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Saved API snapshot to %s (%d kinds, %d operators)", path, len(kinds), len(operators))


def _load_snapshot() -> dict | None:
    """Load the previous snapshot, or None if it doesn't exist."""
    path = _snapshot_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load API snapshot from %s: %s", path, exc)
        return None


def detect_drift(current_kinds: set[str], current_operators: list[str]) -> DriftResult:
    """Compare the current API surface to the last saved snapshot.

    If no previous snapshot exists, saves the current state and returns no drift.
    """
    previous = _load_snapshot()
    if previous is None:
        save_snapshot(current_kinds, current_operators)
        return DriftResult()

    prev_kinds = set(previous.get("kinds", []))
    prev_operators = set(previous.get("operators", []))
    curr_operators = set(current_operators)

    result = DriftResult(
        new_apis=sorted(current_kinds - prev_kinds),
        removed_apis=sorted(prev_kinds - current_kinds),
        new_operators=sorted(curr_operators - prev_operators),
        removed_operators=sorted(prev_operators - curr_operators),
    )

    # Update snapshot with current state
    save_snapshot(current_kinds, current_operators)

    if result.has_breaking_changes:
        logger.warning("API drift detected: %s", result.summary())
    elif result.new_apis or result.new_operators:
        logger.info("API surface changed: %s", result.summary())

    return result


def check_manifests_for_deprecated_apis(
    manifest_dir: Path,
    deprecated: list[dict],
) -> list[dict]:
    """Scan YAML manifests in a directory for usage of deprecated APIs.

    Each entry in ``deprecated`` must have an ``api`` key like
    ``"autoscaling/v2beta1 HorizontalPodAutoscaler"`` (apiVersion + optional Kind).

    Returns a list of issues found.
    """
    issues: list[dict] = []

    # Build a lookup: apiVersion -> deprecation info
    dep_lookup: dict[str, dict] = {}
    for d in deprecated:
        api_str = d.get("api", "")
        # Extract just the apiVersion part (before the space / kind)
        api_version = api_str.split()[0] if api_str else ""
        if api_version:
            dep_lookup[api_version] = d

    if not manifest_dir.is_dir():
        return issues

    for yaml_file in sorted(manifest_dir.rglob("*.yaml")):
        try:
            text = yaml_file.read_text(encoding="utf-8")
            for doc in yaml.safe_load_all(text):
                if not isinstance(doc, dict):
                    continue
                api_version = doc.get("apiVersion", "")
                kind = doc.get("kind", "")
                if not api_version:
                    continue

                if api_version in dep_lookup:
                    dep_info = dep_lookup[api_version]
                    issues.append({
                        "file": str(yaml_file),
                        "api": f"{api_version} {kind}",
                        "deprecated_in": dep_info.get("deprecated_in", "?"),
                        "removed_in": dep_info.get("removed_in", "?"),
                        "replacement": dep_info.get("replacement", ""),
                    })
        except (yaml.YAMLError, OSError) as exc:
            logger.debug("Skipping %s: %s", yaml_file, exc)

    return issues
