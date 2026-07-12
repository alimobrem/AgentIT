"""Data-driven check engine that loads YAML check files and runs them against a repo.

Each check file defines a declarative rule (file_exists, file_contains, file_missing,
yaml_kind_exists, yaml_kind_missing) that produces a Finding when triggered.  The
learning agent can create new check files without touching Python code.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

import yaml

from agentit.analyzers.base import is_ignored, iter_yaml_files
from agentit.models import DimensionScore, Finding, Severity

logger = logging.getLogger(__name__)

VALID_TYPES = frozenset(
    {"file_exists", "file_contains", "file_missing", "yaml_kind_exists", "yaml_kind_missing"}
)

SEVERITY_MAP: dict[str, Severity] = {
    "critical": Severity.critical,
    "high": Severity.high,
    "medium": Severity.medium,
    "low": Severity.low,
    "info": Severity.info,
}


class CheckDefinition:
    """A single parsed check loaded from a YAML file."""

    __slots__ = ("name", "dimension", "severity", "category", "check_type", "pattern",
                 "description", "recommendation", "source_path")

    def __init__(
        self,
        name: str,
        dimension: str,
        severity: Severity,
        category: str,
        check_type: str,
        pattern: str,
        description: str,
        recommendation: str,
        source_path: str,
    ) -> None:
        self.name = name
        self.dimension = dimension
        self.severity = severity
        self.category = category
        self.check_type = check_type
        self.pattern = pattern
        self.description = description
        self.recommendation = recommendation
        self.source_path = source_path


def _parse_check_file(path: Path) -> CheckDefinition | None:
    """Parse a single YAML check file.  Returns ``None`` on invalid input."""
    try:
        data = yaml.safe_load(path.read_text())
    except Exception:
        logger.warning("Failed to parse check file %s", path)
        return None

    if not isinstance(data, dict):
        logger.warning("Check file %s is not a YAML mapping", path)
        return None

    missing = [k for k in ("name", "dimension", "severity", "category", "type", "pattern",
                            "description", "recommendation") if k not in data]
    if missing:
        logger.warning("Check file %s missing keys: %s", path, ", ".join(missing))
        return None

    check_type = data["type"]
    if check_type not in VALID_TYPES:
        logger.warning("Check file %s has unknown type '%s'", path, check_type)
        return None

    sev = SEVERITY_MAP.get(str(data["severity"]).lower())
    if sev is None:
        logger.warning("Check file %s has unknown severity '%s'", path, data["severity"])
        return None

    return CheckDefinition(
        name=data["name"],
        dimension=data["dimension"],
        severity=sev,
        category=data["category"],
        check_type=check_type,
        pattern=str(data["pattern"]),
        description=data["description"],
        recommendation=data["recommendation"],
        source_path=str(path),
    )


def load_checks(checks_dir: Path) -> list[CheckDefinition]:
    """Load all YAML check files from *checks_dir* (recursively)."""
    checks: list[CheckDefinition] = []
    if not checks_dir.is_dir():
        return checks
    for path in sorted(checks_dir.rglob("*.yaml")):
        defn = _parse_check_file(path)
        if defn is not None:
            checks.append(defn)
    for path in sorted(checks_dir.rglob("*.yml")):
        defn = _parse_check_file(path)
        if defn is not None:
            checks.append(defn)
    return checks


# ---------------------------------------------------------------------------
# Check runners
# ---------------------------------------------------------------------------

def _run_file_exists(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if at least one file matches the glob *pattern*."""
    for fp in repo_path.rglob("*"):
        if fp.is_file() and not is_ignored(fp, repo_path):
            if fnmatch.fnmatch(fp.name, check.pattern):
                return None
    # No match -> finding
    return _make_finding(check)


def _run_file_contains(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if any non-ignored text file contains *pattern*."""
    for fp in repo_path.rglob("*"):
        if not fp.is_file() or is_ignored(fp, repo_path):
            continue
        try:
            content = fp.read_text(errors="ignore")
        except OSError:
            continue
        if check.pattern in content:
            return None
    return _make_finding(check)


def _run_file_missing(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return a finding if a file matching the glob *pattern* IS found."""
    for fp in repo_path.rglob("*"):
        if fp.is_file() and not is_ignored(fp, repo_path):
            if fnmatch.fnmatch(fp.name, check.pattern):
                return _make_finding(check, file_path=str(fp.relative_to(repo_path)))
    return None


def _run_yaml_kind_exists(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if any YAML file contains ``kind: <pattern>``."""
    needle = f"kind: {check.pattern}"
    for _, content in iter_yaml_files(repo_path):
        if needle in content:
            return None
    return _make_finding(check)


def _run_yaml_kind_missing(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return a finding if any YAML file contains ``kind: <pattern>``."""
    needle = f"kind: {check.pattern}"
    for path, content in iter_yaml_files(repo_path):
        if needle in content:
            return _make_finding(check, file_path=str(path.relative_to(repo_path)))
    return None


_RUNNERS = {
    "file_exists": _run_file_exists,
    "file_contains": _run_file_contains,
    "file_missing": _run_file_missing,
    "yaml_kind_exists": _run_yaml_kind_exists,
    "yaml_kind_missing": _run_yaml_kind_missing,
}


def _make_finding(check: CheckDefinition, file_path: str | None = None) -> Finding:
    return Finding(
        category=check.category,
        severity=check.severity,
        description=check.description,
        file_path=file_path,
        recommendation=check.recommendation,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_checks(checks: list[CheckDefinition], repo_path: Path) -> list[Finding]:
    """Execute all checks against *repo_path* and return triggered findings."""
    findings: list[Finding] = []
    for check in checks:
        runner = _RUNNERS.get(check.check_type)
        if runner is None:
            continue
        finding = runner(check, repo_path)
        if finding is not None:
            findings.append(finding)
    return findings


def run_checks_by_dimension(
    checks: list[CheckDefinition],
    repo_path: Path,
) -> dict[str, list[Finding]]:
    """Run checks and group the resulting findings by dimension."""
    grouped: dict[str, list[Finding]] = {}
    for check in checks:
        runner = _RUNNERS.get(check.check_type)
        if runner is None:
            continue
        finding = runner(check, repo_path)
        if finding is not None:
            grouped.setdefault(check.dimension, []).append(finding)
    return grouped
