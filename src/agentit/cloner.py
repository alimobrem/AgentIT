from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from git import GitCommandError, Repo


class CloneError(Exception):
    pass


_ALLOWED_SCHEMES = {"https", "http"}
_DANGEROUS_URL_RE = re.compile(r"ext::|--upload-pack|--config")


def _validate_repo_url(repo_url: str) -> None:
    if repo_url.startswith("-"):
        raise CloneError(f"Rejected URL starting with dash: {repo_url}")

    if _DANGEROUS_URL_RE.search(repo_url):
        raise CloneError(f"Rejected URL with dangerous pattern: {repo_url}")

    parsed = urlparse(repo_url)
    if parsed.scheme and parsed.scheme not in _ALLOWED_SCHEMES:
        raise CloneError(
            f"Rejected URL scheme '{parsed.scheme}'. Only https:// and http:// are allowed."
        )


def clone_repo(
    repo_url: str,
    target_dir: Path | None = None,
    branch: str | None = None,
    depth: int = 1,
    allow_local: bool = False,
) -> Path:
    if not allow_local:
        _validate_repo_url(repo_url)

    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="agentit-"))

    kwargs: dict = {"depth": depth}
    if branch:
        kwargs["branch"] = branch

    env = dict(os.environ)
    if not allow_local:
        env["GIT_PROTOCOL_FROM_USER"] = "0"

    try:
        Repo.clone_from(repo_url, str(target_dir), env=env, **kwargs)
    except GitCommandError as exc:
        raise CloneError(f"Failed to clone {repo_url}: {exc}") from exc

    return target_dir
