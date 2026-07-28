"""Container registry client — best-effort real digest resolution.

``source_patches.py``'s Dockerfile hardening used to pin ``:latest`` to
another floating tag (``:1``) and call that "pinned" -- confirmed live on
two fleet apps (pulse-agent, pulse-ui) that this satisfies AgentIT's own
naive "no literal :latest" check without providing real image immutability:
a UBI ``:1`` stream tag still moves underneath you on every Red Hat
patch release, with no signal anything changed. This module resolves the
image's ACTUAL current digest via the target registry's own Docker
Registry HTTP API V2 (the same anonymous-pull + bearer-token flow every
public registry -- registry.access.redhat.com, quay.io, Docker Hub,
ghcr.io -- already supports for public images), so a "pinned" FROM line
means what it claims.

Every failure mode (unreachable registry, private/rate-limited repo, an
auth flow this doesn't implement) degrades to ``None`` -- callers must
fall back to their existing, safe default (the floating ``:1`` tag)
rather than ever fabricating a digest or blocking delivery on a flaky
registry.
"""
from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
_DOCKER_HUB_REGISTRY = "registry-1.docker.io"


def parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Split ``[registry/]repository[:tag]`` into ``(registry,
    repository, tag)`` using standard Docker reference resolution rules:
    a bare ``nginx`` implies Docker Hub's ``library/nginx``; a first path
    segment containing ``.``/``:``, or literally ``localhost``, is a
    registry host rather than a repository owner.

    Returns ``("", "", "")`` for an already digest-pinned reference
    (``...@sha256:...``) -- there is nothing left to resolve.
    """
    ref = (image_ref or "").strip()
    if not ref or "@" in ref:
        return "", "", ""
    tag = "latest"
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        ref, _, tag = ref.rpartition(":")
    parts = ref.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repository = parts[0], parts[1]
    else:
        registry = _DOCKER_HUB_REGISTRY
        repository = ref if "/" in ref else f"library/{ref}"
    return registry, repository, tag


def _anonymous_bearer_token(challenge: str, *, timeout: float) -> str | None:
    """Parse a ``WWW-Authenticate: Bearer realm="...",service="...",
    scope="..."`` challenge and fetch an anonymous pull token -- the
    standard Docker Registry v2 token-auth flow every public registry
    supports for public image pulls, no credentials involved."""
    m = re.match(r'Bearer\s+(.+)', challenge or "")
    if not m:
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    realm = params.get("realm")
    if not realm:
        return None
    try:
        resp = requests.get(
            realm,
            params={k: v for k, v in params.items() if k != "realm"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("token") or data.get("access_token")
    except (requests.RequestException, ValueError):
        pass
    return None


def resolve_image_digest(image_ref: str, *, timeout: float = 4.0) -> str | None:
    """Best-effort real digest for ``image_ref``'s current tag, via the
    target registry's own v2 Manifest API. Returns ``None`` on any
    failure -- never raises; every caller must treat ``None`` as "could
    not verify a real digest," not as an error to surface or retry.
    """
    registry, repository, tag = parse_image_ref(image_ref)
    if not registry or not repository or not tag:
        return None
    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 401:
            token = _anonymous_bearer_token(resp.headers.get("WWW-Authenticate", ""), timeout=timeout)
            if not token:
                return None
            resp = requests.get(
                url, headers={**headers, "Authorization": f"Bearer {token}"}, timeout=timeout,
            )
        if resp.status_code == 200:
            digest = resp.headers.get("Docker-Content-Digest")
            if digest and digest.startswith("sha256:"):
                return digest
    except requests.RequestException:
        logger.info("resolve_image_digest: request failed for %s", image_ref, exc_info=True)
    return None
