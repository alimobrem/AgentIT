# agentit/src/agentit/cloner.py
from __future__ import annotations

import tempfile
from pathlib import Path

from git import GitCommandError, Repo


class CloneError(Exception):
    pass


def clone_repo(
    repo_url: str,
    target_dir: Path | None = None,
    branch: str | None = None,
    depth: int = 1,
) -> Path:
    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="agentit-"))

    kwargs: dict = {"depth": depth}
    if branch:
        kwargs["branch"] = branch

    try:
        Repo.clone_from(repo_url, str(target_dir), **kwargs)
    except GitCommandError as exc:
        raise CloneError(f"Failed to clone {repo_url}: {exc}") from exc

    return target_dir
