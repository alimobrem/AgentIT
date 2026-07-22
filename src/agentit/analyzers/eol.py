"""Deterministic and LLM-assisted end-of-life (EOL) detection for base images
and language runtimes.

The baseline table/scan in this module is always available and never calls
out to an LLM: it matches known EOL / approaching-EOL versions of Python,
Node.js, and common Linux base images (Ubuntu, Debian, CentOS, Alpine)
against real support-lifecycle dates published by each project. When an LLM
client is configured (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_VERTEX_PROJECT_ID``),
``llm_findings()`` additionally asks it to flag anything EOL/near-EOL that
this fixed table doesn't cover (other frameworks/runtimes, less common base
images) -- see ``LLMClient.detect_eol_risks`` in ``agentit.llm``. The LLM
path is purely additive on top of the baseline and degrades to nothing
(baseline stays authoritative) on any failure or absence of an LLM client,
per this repo's LLM conventions (CLAUDE.md: "LLM calls must always fail
gracefully").

EOL dates below are sourced from each project's own lifecycle page (verified
as of 2026-07):
  - Python: https://devguide.python.org/versions/
  - Node.js: https://github.com/nodejs/Release (README.md release schedule)
  - Ubuntu: https://ubuntu.com/about/release-cycle
  - Debian: https://wiki.debian.org/LTS (LTS end date -- the last date any
    security patch, including volunteer LTS, is available)
  - CentOS Linux: https://www.centos.org/centos-linux-eol/
  - Alpine: https://www.alpinelinux.org/releases/ (+ https://endoflife.ai/alpine-linux)
Dates marked "scheduled" are each project's own published target and may
shift; they are still real, cited dates, not fabricated ones.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agentit.models import Finding, Severity

logger = logging.getLogger(__name__)

APPROACHING_EOL_WINDOW_DAYS = 180
LLM_MIN_CONFIDENCE = 0.6

PYTHON_EOL: dict[str, date] = {
    "2.7": date(2020, 1, 1),
    "3.6": date(2021, 12, 23),
    "3.7": date(2023, 6, 27),
    "3.8": date(2024, 10, 7),
    "3.9": date(2025, 10, 31),
    "3.10": date(2026, 10, 31),  # scheduled
    "3.11": date(2027, 10, 31),  # scheduled
    "3.12": date(2028, 10, 31),  # scheduled
}

NODE_EOL: dict[str, date] = {
    "12": date(2022, 4, 30),
    "13": date(2020, 6, 1),
    "14": date(2023, 4, 30),
    "15": date(2021, 6, 1),
    "16": date(2023, 9, 11),
    "17": date(2022, 6, 1),
    "18": date(2025, 4, 30),
    "19": date(2023, 6, 1),
    "20": date(2026, 4, 30),
    "21": date(2024, 6, 1),
}

UBUNTU_EOL: dict[str, date] = {
    "14.04": date(2019, 4, 30),
    "16.04": date(2021, 4, 30),
    "18.04": date(2023, 5, 31),
    "20.04": date(2025, 5, 31),
    "22.04": date(2027, 4, 30),  # scheduled
}

CENTOS_EOL: dict[str, date] = {
    "6": date(2020, 11, 30),
    "7": date(2024, 6, 30),
    "8": date(2021, 12, 31),
}

DEBIAN_EOL: dict[str, date] = {
    "8": date(2020, 6, 30),
    "9": date(2022, 6, 30),
    "10": date(2024, 6, 30),
    "11": date(2026, 8, 31),
    "12": date(2028, 6, 30),  # scheduled
}

ALPINE_EOL: dict[str, date] = {
    "3.9": date(2020, 11, 1),
    "3.10": date(2021, 5, 1),
    "3.11": date(2021, 11, 1),
    "3.12": date(2022, 5, 1),
    "3.13": date(2022, 11, 1),
    "3.14": date(2023, 5, 1),
    "3.15": date(2023, 11, 1),
    "3.16": date(2024, 5, 23),
    "3.17": date(2024, 11, 22),
    "3.18": date(2025, 5, 9),
    "3.19": date(2025, 11, 1),
    "3.20": date(2026, 4, 1),
}

_BASE_IMAGE_TABLES: dict[str, dict[str, date]] = {
    "python": PYTHON_EOL,
    "node": NODE_EOL,
    "ubuntu": UBUNTU_EOL,
    "centos": CENTOS_EOL,
    "debian": DEBIAN_EOL,
    "alpine": ALPINE_EOL,
}

_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE | re.IGNORECASE)
_VERSION_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+){0,2})")
_PY_PYPROJECT_RE = re.compile(r"python\s*=?\s*[\"'^~>=<]*(\d+\.\d+)", re.IGNORECASE)
_PY_RUNTIME_TXT_RE = re.compile(r"python-(\d+\.\d+)", re.IGNORECASE)

_CI_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", "Jenkinsfile")
_LLM_CONTEXT_FILENAMES = (
    "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "package.json", "go.mod", ".python-version", "runtime.txt",
)
_MAX_EXCERPT_CHARS = 4000
_MAX_CONTEXT_FILES = 20


def _status_for(eol_date: date, today: date) -> str | None:
    """Classify *eol_date* relative to *today*: 'eol', 'approaching_eol', or None."""
    if eol_date <= today:
        return "eol"
    if eol_date - today <= timedelta(days=APPROACHING_EOL_WINDOW_DAYS):
        return "approaching_eol"
    return None


def _match_table_version(table: dict[str, date], version: str) -> str | None:
    """Resolve *version* against *table* by exact, then major, then major.minor match."""
    if version in table:
        return version
    parts = version.split(".")
    major = parts[0]
    if major in table:
        return major
    if len(parts) >= 2:
        two_part = f"{parts[0]}.{parts[1]}"
        if two_part in table:
            return two_part
    return None


def _parse_image_ref(ref: str) -> tuple[str, str] | None:
    """Split an image reference's final path segment into (name, tag).

    Returns ``None`` for untagged refs (``:latest`` w/o digit tag, or bare
    names) -- those are already covered by the security analyzer's
    ``:latest`` check, not this EOL scan.
    """
    ref = ref.split("@", 1)[0]
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None
    name, tag = last_segment.rsplit(":", 1)
    return name.lower(), tag


def _make_baseline_finding(component: str, version: str, status: str, eol_date: date, file_path: str | None) -> Finding:
    if status == "eol":
        severity = Severity.high
        verb = "is past end-of-life"
    else:
        severity = Severity.medium
        verb = "is approaching end-of-life"
    return Finding(
        category="eol",
        severity=severity,
        description=f"{component} {version} {verb} (EOL {eol_date.isoformat()})",
        file_path=file_path,
        recommendation=f"Upgrade {component} past version {version} ahead of its {eol_date.isoformat()} end-of-life date",
        source="analyzer:infrastructure:eol-baseline",
    )


def _scan_dockerfiles(repo_path: Path, today: date) -> list[Finding]:
    findings: list[Finding] = []
    dockerfiles = sorted(list(repo_path.glob("Dockerfile*")) + list(repo_path.glob("Containerfile*")))
    for df in dockerfiles:
        try:
            content = df.read_text(errors="ignore")
        except OSError:
            continue
        rel_path = str(df.relative_to(repo_path))
        for match in _FROM_RE.finditer(content):
            parsed = _parse_image_ref(match.group(1))
            if parsed is None:
                continue
            name, tag = parsed
            table = _BASE_IMAGE_TABLES.get(name)
            if table is None:
                continue
            version_match = _VERSION_PREFIX_RE.match(tag)
            if not version_match:
                continue
            resolved = _match_table_version(table, version_match.group(1))
            if resolved is None:
                continue
            status = _status_for(table[resolved], today)
            if status is None:
                continue
            findings.append(_make_baseline_finding(name, resolved, status, table[resolved], rel_path))
    return findings


def _scan_python_version_files(repo_path: Path, today: date) -> list[Finding]:
    """Check ``.python-version``, ``runtime.txt``, and ``pyproject.toml`` in
    that priority order (most specific/unambiguous first) for a pinned
    Python version -- the same three files ``StackDetector`` already reads
    for its own version detection."""
    candidates: list[tuple[str, re.Pattern]] = [
        (".python-version", re.compile(r"(\d+\.\d+)")),  # whole file is the version
        ("runtime.txt", _PY_RUNTIME_TXT_RE),
        ("pyproject.toml", _PY_PYPROJECT_RE),
    ]
    for filename, pattern in candidates:
        p = repo_path / filename
        if not p.exists():
            continue
        try:
            content = p.read_text(errors="ignore")
        except OSError:
            continue
        match = pattern.search(content)
        if not match:
            continue
        resolved = _match_table_version(PYTHON_EOL, match.group(1))
        if resolved is None:
            return []
        status = _status_for(PYTHON_EOL[resolved], today)
        if status is None:
            return []
        return [_make_baseline_finding("python", resolved, status, PYTHON_EOL[resolved], filename)]
    return []


def _scan_node_version_pin(repo_path: Path) -> str | None:
    """Return major Node version from .node-version / .nvmrc when present."""
    for name in (".node-version", ".nvmrc"):
        p = repo_path / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore").strip()
        except OSError:
            continue
        # Allow "22", "v22", "22.11.0"
        match = re.search(r"v?(\d+)", text.split("\n", 1)[0])
        if match:
            return match.group(1)
    return None


def _scan_package_json(repo_path: Path, today: date) -> list[Finding]:
    # Runtime pin files take precedence over package.json engines — source
    # remediations emit .node-version without destroying package.json.
    pinned = _scan_node_version_pin(repo_path)
    if pinned is not None:
        resolved = _match_table_version(NODE_EOL, pinned)
        if resolved is None:
            return []
        status = _status_for(NODE_EOL[resolved], today)
        if status is None:
            return []
        return [_make_baseline_finding(
            "node", resolved, status, NODE_EOL[resolved], ".node-version",
        )]

    p = repo_path / "package.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    engines = data.get("engines")
    node_spec = engines.get("node") if isinstance(engines, dict) else None
    if not node_spec:
        return []
    match = re.search(r"(\d+)", str(node_spec))
    if not match:
        return []
    resolved = _match_table_version(NODE_EOL, match.group(1))
    if resolved is None:
        return []
    status = _status_for(NODE_EOL[resolved], today)
    if status is None:
        return []
    return [_make_baseline_finding("node", resolved, status, NODE_EOL[resolved], "package.json")]


def baseline_findings(repo_path: Path, today: date | None = None) -> list[Finding]:
    """Always-available, deterministic EOL scan (no LLM). Real EOL dates
    only -- see module docstring for sources."""
    resolved_today = today or datetime.now(timezone.utc).date()
    findings: list[Finding] = []
    findings.extend(_scan_dockerfiles(repo_path, resolved_today))
    findings.extend(_scan_python_version_files(repo_path, resolved_today))
    findings.extend(_scan_package_json(repo_path, resolved_today))
    return findings


# ---------------------------------------------------------------------------
# LLM-assisted path (additive, degrades to nothing on any failure)
# ---------------------------------------------------------------------------


def _collect_llm_context(repo_path: Path) -> dict[str, str]:
    """Gather (truncated) content of files relevant to EOL reasoning: base
    image definitions, dependency manifests, and CI config."""
    excerpts: dict[str, str] = {}

    for df in list(repo_path.glob("Dockerfile*")) + list(repo_path.glob("Containerfile*")):
        if df.is_file():
            try:
                excerpts[str(df.relative_to(repo_path))] = df.read_text(errors="ignore")[:_MAX_EXCERPT_CHARS]
            except OSError:
                continue

    for filename in _LLM_CONTEXT_FILENAMES:
        p = repo_path / filename
        if p.is_file():
            try:
                excerpts[str(p.relative_to(repo_path))] = p.read_text(errors="ignore")[:_MAX_EXCERPT_CHARS]
            except OSError:
                continue

    for pattern in _CI_GLOBS:
        for p in repo_path.glob(pattern):
            if len(excerpts) >= _MAX_CONTEXT_FILES:
                break
            if p.is_file():
                try:
                    excerpts[str(p.relative_to(repo_path))] = p.read_text(errors="ignore")[:_MAX_EXCERPT_CHARS]
                except OSError:
                    continue

    return excerpts


def llm_findings(repo_path: Path, llm_client: object, stack_info: dict) -> list[Finding] | None:
    """Ask the LLM to flag any EOL/near-EOL component across the repo's
    detected stack that the deterministic baseline doesn't cover.

    Returns ``None`` if the LLM is unavailable, failed, or returned
    something this code can't trust -- callers must treat ``None`` as "no
    additional findings" and keep relying on ``baseline_findings()``, never
    as "confirmed clean". Returns ``[]`` for a real "the LLM looked and
    found nothing" answer.
    """
    excerpts = _collect_llm_context(repo_path)
    if not excerpts:
        return []

    risks = llm_client.detect_eol_risks(stack_info, excerpts)
    if risks is None:
        return None
    if not isinstance(risks, list):
        logger.warning("LLM EOL client returned a non-list response: %r", type(risks).__name__)
        return None

    findings: list[Finding] = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        confidence = risk.get("confidence")
        if not isinstance(confidence, (int, float)) or confidence < LLM_MIN_CONFIDENCE:
            continue
        component = risk.get("component")
        version = risk.get("version")
        status = risk.get("status")
        if not component or not version or status not in ("eol", "approaching_eol"):
            continue
        eol_date_str = risk.get("eol_date") or "unspecified date"
        reason = str(risk.get("reason") or "").strip()
        severity = Severity.high if status == "eol" else Severity.medium
        verb = "is past end-of-life" if status == "eol" else "is approaching end-of-life"
        description = f"{component} {version} {verb} (EOL {eol_date_str})"
        if reason:
            description = f"{description}: {reason}"
        findings.append(Finding(
            category="eol",
            severity=severity,
            description=description,
            file_path=None,
            recommendation=f"Upgrade {component} past version {version}",
            source="analyzer:infrastructure:eol-llm",
        ))
    return findings
