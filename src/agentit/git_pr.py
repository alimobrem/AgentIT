"""Shared git branch/commit/push mechanics, plus opening a draft PR via `gh`.

Extracted from `cli.py`'s `self-fix --create-pr` command -- the first (and,
until `capability_scout.py`, only) code path in this repo that branches/
commits/pushes AgentIT's own checked-out working tree, as opposed to
`portal/github_pr.py`'s GitHub-Contents-API helpers, which write generated
*manifests* into a *target app's* repo via the raw REST API (a different
problem: no local git working tree involved at all). `capability_scout.py`
reuses this exact path rather than re-implementing branch/push/PR-open logic
a third time -- see docs/self-improvement-for-agentit.md.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def create_branch_commit_push(
    branch: str,
    paths: list[str],
    commit_message: str,
    cwd: Path | None = None,
) -> dict:
    """`git checkout -b` / `git add` / `git commit` / `git push -u origin <branch>`.

    Same shape as `self-fix --create-pr`'s existing subprocess calls
    (`cli.py`), just parameterized and reusable. Returns
    ``{"success": True, "branch": ...}`` or ``{"success": False, "error": ...}``
    -- never raises, since a git/push failure is a normal, expected outcome
    for a non-interactive caller (the safety-gate "discard, don't crash"
    path), not something that should take down a watcher's tick.
    """
    try:
        subprocess.run(["git", "checkout", "-b", branch], check=True, capture_output=True, cwd=cwd, text=True)
        subprocess.run(["git", "add", *paths], check=True, capture_output=True, cwd=cwd, text=True)
        subprocess.run(["git", "commit", "-m", commit_message], check=True, capture_output=True, cwd=cwd, text=True)
        subprocess.run(["git", "push", "-u", "origin", branch], check=True, capture_output=True, cwd=cwd, text=True)
        return {"success": True, "branch": branch}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else str(exc.stderr or "")
        logger.warning("git branch/commit/push failed for branch %s: %s", branch, stderr)
        return {"success": False, "error": stderr[:500] or str(exc)}
    except OSError as exc:
        logger.warning("git command unavailable: %s", exc)
        return {"success": False, "error": str(exc)}


def open_draft_pr(
    branch: str,
    title: str,
    body: str,
    base: str = "main",
    cwd: Path | None = None,
) -> dict:
    """`gh pr create --draft` -- the actual PR-open step.

    Requires the `gh` CLI to be installed and authenticated (`GITHUB_TOKEN`
    or a prior `gh auth login`) in this process's environment -- per
    docs/self-improvement-for-agentit.md, `gh` handles create-branch-and-PR
    in one call and needs no extra credential beyond what PR creation
    already requires. Never raises: a missing `gh` binary or an auth
    failure returns ``{"error": ...}`` so a watcher tick can log a clean,
    non-fatal outcome instead of crashing.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "create", "--draft", "--title", title, "--body", body,
             "--head", branch, "--base", base],
            capture_output=True, text=True, cwd=cwd, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("gh pr create unavailable/failed: %s", exc)
        return {"error": str(exc)}
    if result.returncode != 0:
        logger.warning("gh pr create failed: %s", result.stderr[:500])
        return {"error": result.stderr[:500] or "gh pr create failed"}
    stdout = result.stdout.strip()
    pr_url = stdout.splitlines()[-1] if stdout else ""
    return {"pr_url": pr_url}
