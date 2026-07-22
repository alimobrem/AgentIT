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


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def resolve_public_ips(hostname: str) -> list[str]:
    """Resolve hostname once; fail closed on DNS errors or private answers.

    TLS hostname verification prevents cloning via a literal pinned IP for
    HTTPS remotes (cert SAN mismatch). Residual TOCTOU after the last
    resolve is mitigated in production by egress controls — see
    docs/adr/0005-ssrf-clone.md.
    """
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if _is_blocked_ip(addr):
            raise CloneError(f"Rejected URL with private/internal host: {hostname}")
        return [str(addr)]

    lower = hostname.lower()
    if any(lower.endswith(s) for s in _INTERNAL_SUFFIXES):
        raise CloneError(f"Rejected URL with private/internal host: {hostname}")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise CloneError(f"Rejected URL: DNS lookup failed for {hostname}: {exc}") from exc

    if not infos:
        raise CloneError(f"Rejected URL: no addresses for {hostname}")

    public: list[str] = []
    seen: set[str] = set()
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise CloneError(
                f"Rejected URL with private/internal host: {hostname} → {ip}"
            )
        key = str(ip)
        if key not in seen:
            seen.add(key)
            public.append(key)

    if not public:
        raise CloneError(f"Rejected URL: no usable addresses for {hostname}")
    return public


def _assert_resolution_still_public(hostname: str) -> list[str]:
    return resolve_public_ips(hostname)


def _validate_repo_url(repo_url: str) -> list[str]:
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

    return resolve_public_ips(hostname)


def clone_repo(
    repo_url: str,
    target_dir: Path | None = None,
    branch: str | None = None,
    depth: int = 1,
    allow_local: bool = False,
) -> Path:
    if not allow_local:
        _validate_repo_url(repo_url)
        hostname = urlparse(repo_url).hostname
        if hostname:
            _assert_resolution_still_public(hostname)

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
