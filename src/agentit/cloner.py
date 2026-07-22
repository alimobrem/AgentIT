from __future__ import annotations

import ipaddress
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from git import GitCommandError, Repo


class CloneError(Exception):
    pass


# HTTPS only — cleartext http:// is rejected.
_ALLOWED_SCHEMES = {"https"}
_DANGEROUS_URL_RE = re.compile(r"ext::|--upload-pack|--config")
_INTERNAL_SUFFIXES = (".internal", ".local", ".corp", ".lan", ".svc")


def _assert_public_resolution(hostname: str) -> None:
    """Resolve hostname; fail closed on DNS errors or any private answer.

    ``git clone`` will re-resolve at fetch time (residual TOCTOU / DNS
    rebinding). Production should also egress-block RFC1918 + 169.254/16
    via NetworkPolicy. This check still stops literal private hosts, suffix
    tricks, and current private DNS answers at validation time.
    """
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise CloneError(f"Rejected URL with private/internal host: {hostname}")
        return

    lower = hostname.lower()
    if any(lower.endswith(s) for s in _INTERNAL_SUFFIXES):
        raise CloneError(f"Rejected URL with private/internal host: {hostname}")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise CloneError(f"Rejected URL: DNS lookup failed for {hostname}: {exc}") from exc

    if not infos:
        raise CloneError(f"Rejected URL: no addresses for {hostname}")

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise CloneError(
                f"Rejected URL with private/internal host: {hostname} → {ip}"
            )


def _validate_repo_url(repo_url: str) -> None:
    if repo_url.startswith("-"):
        raise CloneError(f"Rejected URL starting with dash: {repo_url}")

    if _DANGEROUS_URL_RE.search(repo_url):
        raise CloneError(f"Rejected URL with dangerous pattern: {repo_url}")

    parsed = urlparse(repo_url)
    if parsed.scheme and parsed.scheme not in _ALLOWED_SCHEMES:
        raise CloneError(
            f"Rejected URL scheme '{parsed.scheme}'. Only https:// is allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        raise CloneError("Rejected URL: missing hostname")

    _assert_public_resolution(hostname)


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
