"""Base-image patching — moved out of agents/hardening.py when that agent
was removed in favor of skills (see docs/agent-removal-readiness.md).

Unlike the rest of hardening.py's generation logic, this isn't
findings-driven manifest generation a skill can replace: it patches an
*existing* Containerfile's FROM line in place, which the skill engine has
no equivalent for. RemediationDispatcher still needs it for the
``base_image`` finding category (triggered by the image-scan Tekton Task's
notify-cve step).
"""
from __future__ import annotations

import re

_UBI_MAP: dict[str, str] = {
    "python": "registry.access.redhat.com/ubi9/python-312:latest",
    "go": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
    "java": "registry.access.redhat.com/ubi9/openjdk-21:latest",
    "node": "registry.access.redhat.com/ubi9/nodejs-20:latest",
    "javascript": "registry.access.redhat.com/ubi9/nodejs-20:latest",
    "typescript": "registry.access.redhat.com/ubi9/nodejs-20:latest",
}


def patch_base_image(dockerfile_content: str, language: str) -> str | None:
    """Replace a non-UBI base image with the UBI equivalent.

    For multi-stage builds, only patches the last FROM (runtime stage).
    Returns the patched content, or None if already UBI.
    """
    lines = dockerfile_content.splitlines(keepends=True)
    from_indices = [
        i for i, line in enumerate(lines)
        if re.match(r"^\s*FROM\s+", line, re.IGNORECASE)
    ]
    if not from_indices:
        return None

    last_from_idx = from_indices[-1]
    from_line = lines[last_from_idx]

    if any(kw in from_line.lower() for kw in ("ubi", "redhat", "registry.access.redhat")):
        return None

    match = re.match(r"^(\s*FROM\s+)(\S+)(.*)", from_line, re.IGNORECASE)
    if not match:
        return None

    ubi_image = _UBI_MAP.get(language.lower(), "registry.access.redhat.com/ubi9/ubi-minimal:latest")
    lines[last_from_idx] = f"{match.group(1)}{ubi_image}{match.group(3)}"
    if not lines[last_from_idx].endswith("\n"):
        lines[last_from_idx] += "\n"

    return "".join(lines)
