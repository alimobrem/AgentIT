"""Runtime write-guard for capability-scout source-mode file creation.

OpenShift runs scout under an arbitrary UID with gid 0. Image ``COPY`` trees
can land root-owned; the Containerfile ``chmod g+w`` on L3 allowlist dirs is
the primary fix. This module is the runtime half: pre-flight writability
before ``write_text``, best-effort ``chmod g+w`` on parent dirs, and a clear
error instead of an uncaught ``PermissionError`` that marks the whole tick
failed and steers the next cycle into docs-only write-guard proposals.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Same trees capability_scout.check_scope_allowlist permits for source diffs.
ALLOWLIST_PREFIXES = (
    "src/agentit/",
    "skills/",
    "checks/",
    "tests/",
    "docs/",
)


def normalize_repo_path(path: str | Path) -> str:
    """POSIX-ish relative path with forward slashes, no leading ``./``."""
    text = str(path).replace("\\", "/").lstrip("./")
    return text


def is_under_allowlist(rel_path: str) -> bool:
    normalized = normalize_repo_path(rel_path)
    return any(normalized.startswith(prefix) for prefix in ALLOWLIST_PREFIXES)


def is_writable(path: Path) -> bool:
    """True when ``path`` can be created or overwritten by this process.

    For a non-existent path, checks that the nearest existing ancestor is a
    writable directory (so a new file can be created there). For an existing
    file, requires write access on the file itself.
    """
    path = Path(path)
    try:
        if path.exists():
            if path.is_dir():
                return os.access(path, os.W_OK | os.X_OK)
            return os.access(path, os.W_OK)
        parent = path.parent
        while not parent.exists():
            if parent == parent.parent:
                return False
            parent = parent.parent
        return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except OSError:
        return False


def _try_chmod_group_writable(path: Path) -> bool:
    """Best-effort ``chmod g+w`` (and u+w) on an existing path. Returns True on success."""
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWGRP | stat.S_IWUSR)
        return True
    except OSError as exc:
        logger.debug("write_guard chmod failed for %s: %s", path, exc)
        return False


def ensure_writable(path: Path) -> tuple[bool, str]:
    """Ensure ``path`` (file) can be written: mkdir parents, chmod, re-check.

    Returns ``(True, "")`` on success, else ``(False, remediation_detail)``.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create parent dir for {path}: {exc}"

    if is_writable(path):
        return True, ""

    # Retry after making the parent (and file, if present) group-writable.
    for candidate in (path.parent, path if path.exists() else None):
        if candidate is not None:
            _try_chmod_group_writable(candidate)

    if is_writable(path):
        return True, ""

    return (
        False,
        f"Permission denied writing {path}: parent mode="
        f"{oct(path.parent.stat().st_mode) if path.parent.exists() else 'missing'}; "
        f"ensure Containerfile chmod g+w on allowlist trees "
        f"(tests/skills/checks/src/docs) and gid 0 at runtime",
    )


def filter_writable(paths: list[Path]) -> list[Path]:
    """Return only paths that pass :func:`is_writable` (no mutation)."""
    writable: list[Path] = []
    for path in paths:
        if is_writable(Path(path)):
            writable.append(Path(path))
        else:
            logger.warning("write_guard: skipping unwritable path %s", path)
    return writable


def filter_stale_permission_tick_failures(
    tick_failures: list[dict] | None,
    repo_dir: Path | None = None,
) -> list[dict]:
    """Drop permission-denied tick failures whose target path is writable now.

    Stale EACCES rows otherwise dominate evidence and steer scout into
    docs-only write-guard proposals after the image/chart fix has already
    landed.
    """
    from agentit.tick_failure_classifier import classify

    repo_dir = Path(repo_dir or ".")
    out: list[dict] = []
    for event in tick_failures or []:
        classified = classify(event if isinstance(event, dict) else {})
        if classified.get("error_class") != "permission_denied":
            out.append(event)
            continue
        affected = classified.get("affected_path")
        if not affected:
            out.append(event)
            continue
        path = Path(affected)
        if not path.is_absolute():
            path = repo_dir / path
        # Also treat "would-be new file under a writable allowlist dir" as fixed.
        if is_writable(path) or is_writable(path.parent):
            logger.info(
                "write_guard: dropping remediated permission_denied tick failure for %s",
                affected,
            )
            continue
        out.append(event)
    return out


def write_diff_files(repo_dir: Path, diff: dict[str, str]) -> tuple[bool, str]:
    """Write every path in ``diff`` under ``repo_dir`` after writability checks.

    On the first unwritable path, returns ``(False, detail)`` without writing
    further files (avoids half-applied source trees before git commit).
    """
    repo_dir = Path(repo_dir)
    planned: list[tuple[Path, str]] = []
    for rel, content in diff.items():
        full = repo_dir / normalize_repo_path(rel)
        ok, detail = ensure_writable(full)
        if not ok:
            return False, detail
        planned.append((full, content))

    for full, content in planned:
        try:
            full.write_text(content, encoding="utf-8")
        except OSError as exc:
            return False, f"write failed for {full}: {exc}"
    return True, ""
