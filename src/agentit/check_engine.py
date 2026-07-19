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
    """A single parsed check loaded from a YAML file (or, via
    `skill_engine.detect_check_definitions()`, adapted from a `mode: detect`
    Markdown skill -- see docs/extension-model-unification-plan-2026-07-18.md.
    Either source produces one of these, run through the exact same runners
    below, so assessment behaves identically regardless of which format
    defined the rule."""

    __slots__ = ("name", "dimension", "severity", "category", "check_type", "pattern",
                 "description", "recommendation", "source_path", "case_insensitive")

    def __init__(
        self,
        name: str,
        dimension: str,
        severity: Severity,
        category: str,
        check_type: str,
        pattern: str | list[str],
        description: str,
        recommendation: str,
        source_path: str,
        case_insensitive: bool = False,
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
        self.case_insensitive = case_insensitive


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

    raw_pattern = data["pattern"]
    pattern: str | list[str] = (
        [str(p) for p in raw_pattern] if isinstance(raw_pattern, list) else str(raw_pattern)
    )

    return CheckDefinition(
        name=data["name"],
        dimension=data["dimension"],
        severity=sev,
        category=data["category"],
        check_type=check_type,
        pattern=pattern,
        description=data["description"],
        recommendation=data["recommendation"],
        source_path=str(path),
        case_insensitive=bool(data.get("case_insensitive", False)),
    )


def load_checks(checks_dir: Path) -> list[CheckDefinition]:
    """Load all YAML check files from *checks_dir* (recursively)."""
    checks: list[CheckDefinition] = []
    if not checks_dir.is_dir():
        return checks
    for path in sorted(p for ext in ("*.yaml", "*.yml") for p in checks_dir.rglob(ext)):
        defn = _parse_check_file(path)
        if defn is not None:
            checks.append(defn)
    return checks


# ---------------------------------------------------------------------------
# Check runners
# ---------------------------------------------------------------------------

def _pattern_list(pattern: str | list[str]) -> list[str]:
    """Normalize a ``CheckDefinition.pattern`` (scalar or list) into a list
    so every runner below applies OR semantics ("matches if any element
    matches") the same way, regardless of whether the rule came from a
    single-string legacy check or a list-pattern one."""
    return pattern if isinstance(pattern, list) else [pattern]


def _run_file_exists(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if at least one file matches any glob in *pattern*."""
    patterns = _pattern_list(check.pattern)
    for fp in repo_path.rglob("*"):
        if fp.is_file() and not is_ignored(fp, repo_path):
            if any(fnmatch.fnmatch(fp.name, p) for p in patterns):
                return None
    # No match -> finding
    return _make_finding(check)


def _run_file_contains(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if any non-ignored text file contains any
    pattern in *pattern* (case-insensitively if ``check.case_insensitive``)."""
    patterns = _pattern_list(check.pattern)
    if check.case_insensitive:
        patterns = [p.lower() for p in patterns]
    for fp in repo_path.rglob("*"):
        if not fp.is_file() or is_ignored(fp, repo_path):
            continue
        try:
            content = fp.read_text(errors="ignore")
        except OSError:
            continue
        haystack = content.lower() if check.case_insensitive else content
        if any(p in haystack for p in patterns):
            return None
    return _make_finding(check)


def _run_file_missing(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return a finding if a file matching any glob in *pattern* IS found."""
    patterns = _pattern_list(check.pattern)
    for fp in repo_path.rglob("*"):
        if fp.is_file() and not is_ignored(fp, repo_path):
            if any(fnmatch.fnmatch(fp.name, p) for p in patterns):
                return _make_finding(check, file_path=str(fp.relative_to(repo_path)))
    return None


def _run_yaml_kind_exists(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if any YAML file contains ``kind: <p>`` for
    any p in *pattern* (case-insensitively if ``check.case_insensitive``)."""
    patterns = _pattern_list(check.pattern)
    needles = [f"kind: {p}" for p in patterns]
    if check.case_insensitive:
        needles = [n.lower() for n in needles]
    for _, content in iter_yaml_files(repo_path):
        haystack = content.lower() if check.case_insensitive else content
        if any(n in haystack for n in needles):
            return None
    return _make_finding(check)


def _run_yaml_kind_missing(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return a finding if any YAML file contains ``kind: <p>`` for any p
    in *pattern* (case-insensitively if ``check.case_insensitive``)."""
    patterns = _pattern_list(check.pattern)
    needles = [f"kind: {p}" for p in patterns]
    if check.case_insensitive:
        needles = [n.lower() for n in needles]
    for path, content in iter_yaml_files(repo_path):
        haystack = content.lower() if check.case_insensitive else content
        if any(n in haystack for n in needles):
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
        source=f"check:{check.source_path}",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_checks(checks: list[CheckDefinition], repo_path: Path) -> list[Finding]:
    """Execute all checks against *repo_path* and return triggered findings."""
    return [f for fl in run_checks_by_dimension(checks, repo_path).values() for f in fl]


def run_checks_by_dimension_with_status(
    checks: list[CheckDefinition],
    repo_path: Path,
) -> tuple[dict[str, list[Finding]], list[dict]]:
    """Run every check once, returning both the failed-check findings (grouped
    by dimension, as `run_checks_by_dimension` does) and a pass/fail row for
    *every* check (as `run_checks_with_status` does).

    Both `run_checks_by_dimension` and `run_checks_with_status` delegate here
    so callers that need one or the other don't pay for running checks twice,
    while callers that need both (`runner.run_assessment`) can call this
    directly.
    """
    grouped: dict[str, list[Finding]] = {}
    statuses: list[dict] = []
    for check in checks:
        runner = _RUNNERS.get(check.check_type)
        if runner is None:
            continue
        finding = runner(check, repo_path)
        statuses.append({
            "check_name": check.name,
            "dimension": check.dimension,
            "passed": finding is None,
        })
        if finding is not None:
            grouped.setdefault(check.dimension, []).append(finding)
    return grouped, statuses


def run_checks_by_dimension(
    checks: list[CheckDefinition],
    repo_path: Path,
) -> dict[str, list[Finding]]:
    """Run checks and group the resulting findings by dimension."""
    grouped, _ = run_checks_by_dimension_with_status(checks, repo_path)
    return grouped


def run_checks_with_status(checks: list[CheckDefinition], repo_path: Path) -> list[dict]:
    """Run every check and report pass/fail for each, regardless of outcome.

    Unlike `run_checks`/`run_checks_by_dimension`, which only emit a result
    for *failed* checks (a Finding), this returns one row per check so a
    pass/fail snapshot can be persisted per assessment (see
    `AssessmentStore.save_check_results`).
    """
    _, statuses = run_checks_by_dimension_with_status(checks, repo_path)
    return statuses
