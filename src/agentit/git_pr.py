"""Shared git branch/commit/push mechanics, plus opening a draft PR via the
GitHub REST API (``portal/github_pr.open_draft_pull_request``).

Extracted from `cli.py`'s `self-fix --create-pr` command -- the first (and,
until `capability_scout.py`, only) code path in this repo that branches/
commits/pushes AgentIT's own checked-out working tree, as opposed to
`portal/github_pr.py`'s GitHub-Contents-API helpers, which write generated
*manifests* into a *target app's* repo via the raw REST API (a different
problem: no local git working tree involved at all). `capability_scout.py`
reuses this exact path rather than re-implementing branch/push/PR-open logic
a third time -- see docs/self-improvement-for-agentit.md.

Local ``git`` subprocess remains for checkout/add/commit/push of the
working tree (no Contents-API equivalent for a dirty scout checkout).
PR *create* no longer shells out to ``gh``.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# `git commit` needs an identity and falls back to guessing one from
# getpwuid()/hostname when user.name/user.email aren't configured -- not
# guaranteed to succeed (or to be fast) in every runtime, e.g. an arbitrary
# OpenShift UID with no /etc/passwd entry. Setting these env vars for the
# commit step only (checkout/add/push don't need an identity) makes this
# automated commit path never depend on ambient git config, consistent with
# the bot identity already baked into Containerfile's `git config --global`.
_BOT_GIT_ENV = {
    "GIT_AUTHOR_NAME": "AgentIT",
    "GIT_AUTHOR_EMAIL": "agentit@agentit.local",
    "GIT_COMMITTER_NAME": "AgentIT",
    "GIT_COMMITTER_EMAIL": "agentit@agentit.local",
}


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
        subprocess.run(
            ["git", "checkout", "-b", branch], check=True, capture_output=True, cwd=cwd, text=True, timeout=30,
        )
        subprocess.run(["git", "add", *paths], check=True, capture_output=True, cwd=cwd, text=True, timeout=30)
        subprocess.run(
            ["git", "commit", "-m", commit_message], check=True, capture_output=True, cwd=cwd, text=True,
            timeout=30, env={**os.environ, **_BOT_GIT_ENV},
        )
        subprocess.run(
            ["git", "push", "-u", "origin", branch], check=True, capture_output=True, cwd=cwd, text=True, timeout=60,
        )
        return {"success": True, "branch": branch}
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else str(exc.stderr or "")
        logger.warning("git branch/commit/push failed for branch %s: %s", branch, stderr)
        return {"success": False, "error": stderr[:500] or str(exc)}
    except subprocess.TimeoutExpired as exc:
        logger.warning("git command timed out for branch %s: %s", branch, exc)
        return {"success": False, "error": str(exc)}
    except OSError as exc:
        logger.warning("git command unavailable: %s", exc)
        return {"success": False, "error": str(exc)}


def open_draft_pr(
    branch: str,
    title: str,
    body: str,
    base: str = "main",
    cwd: Path | None = None,
    repo_url: str | None = None,
) -> dict:
    """Open a draft PR via the GitHub REST API (no ``gh`` CLI).

    Requires ``GITHUB_TOKEN``. Resolves ``repo_url`` from the argument,
    ``AGENTIT_REPO_URL`` / ``GITHUB_REPOSITORY``, or ``git remote origin``
    in ``cwd``. Never raises: auth/API failures return ``{"error": ...}``
    so a watcher tick can log a clean, non-fatal outcome instead of crashing.
    """
    from agentit.portal.github_pr import open_draft_pull_request, resolve_agentit_repo_url

    url = (repo_url or "").strip() or resolve_agentit_repo_url(cwd)
    return open_draft_pull_request(
        url, head=branch, title=title, body=body, base=base,
    )
