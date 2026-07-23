"""Source patches for Deployment/Rollout replicas and health probes.

Assess ``replicas`` / ``health`` findings scan app-repo YAML. Clear-evidence
requires the same shapes in staged files — not a Kyverno mutate Policy alone.
"""
from __future__ import annotations

import re
from typing import Any

_KIND_WORKLOAD = re.compile(
    r"^\s*kind:\s*(Deployment|Rollout)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_REPLICAS_LINE = re.compile(
    r"^([ \t]*)replicas:\s*[\"']?(\d+)[\"']?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_LIVENESS = re.compile(r"^\s*livenessProbe\s*:", re.IGNORECASE | re.MULTILINE)
_READINESS = re.compile(r"^\s*readinessProbe\s*:", re.IGNORECASE | re.MULTILINE)
_CONTAINERS = re.compile(
    r"^([ \t]*)containers:\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def is_workload_manifest(content: str) -> bool:
    return bool(_KIND_WORKLOAD.search(content or ""))


def replicas_at_least(content: str, minimum: int = 2) -> bool:
    """True when a Deployment/Rollout declares replicas >= minimum."""
    if not is_workload_manifest(content or ""):
        return False
    m = _REPLICAS_LINE.search(content or "")
    if not m:
        return False
    try:
        return int(m.group(2)) >= minimum
    except ValueError:
        return False


def has_health_probes(content: str) -> bool:
    """True when workload YAML declares both liveness and readiness probes."""
    if not is_workload_manifest(content or ""):
        return False
    body = content or ""
    return bool(_LIVENESS.search(body) and _READINESS.search(body))


def patch_replicas(content: str, *, replicas: int = 2) -> str:
    """Set ``spec.replicas`` to at least ``replicas`` on a workload manifest."""
    body = content if (content or "").endswith("\n") else (content or "") + "\n"
    if not is_workload_manifest(body):
        return body

    def _bump(match: re.Match[str]) -> str:
        indent, current = match.group(1), match.group(2)
        try:
            cur = int(current)
        except ValueError:
            cur = 0
        return f"{indent}replicas: {max(cur, replicas)}"

    if _REPLICAS_LINE.search(body):
        return _REPLICAS_LINE.sub(_bump, body, count=1)

    # Insert replicas under first ``spec:`` block.
    spec_m = re.search(r"^([ \t]*)spec:\s*$", body, re.MULTILINE)
    if not spec_m:
        return body
    indent = spec_m.group(1) + "  "
    insert_at = spec_m.end()
    return body[:insert_at] + f"\n{indent}replicas: {replicas}" + body[insert_at:]


_PROBE_SNIPPET = """\
{indent}livenessProbe:
{indent}  tcpSocket:
{indent}    port: {port}
{indent}  initialDelaySeconds: 15
{indent}  periodSeconds: 20
{indent}readinessProbe:
{indent}  tcpSocket:
{indent}    port: {port}
{indent}  initialDelaySeconds: 5
{indent}  periodSeconds: 10
"""


def _container_port(content: str) -> int:
    m = re.search(
        r"containerPort:\s*(\d+)",
        content or "",
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 8080


def patch_health_probes(content: str) -> str:
    """Inject liveness/readiness probes under the first containers entry.

    Uses tcpSocket on an already-declared containerPort (default 8080).
    Idempotent when probes already present.
    """
    body = content if (content or "").endswith("\n") else (content or "") + "\n"
    if not is_workload_manifest(body) or has_health_probes(body):
        return body

    port = _container_port(body)
    c_m = _CONTAINERS.search(body)
    if not c_m:
        return body
    # Find first list item under containers (- name: …)
    after = body[c_m.end():]
    item_m = re.search(r"\n([ \t]*)-\s+", after)
    if not item_m:
        return body
    item_indent = item_m.group(1)
    field_indent = item_indent + "  "
    # Insert probes before the next peer list item or end of containers block.
    # Place after the first container's name/image block: after first non-empty
    # indented fields, before next ``- `` at same indent.
    start = c_m.end() + item_m.start() + 1  # at the '-'
    rest = body[start:]
    next_peer = re.search(
        rf"\n{re.escape(item_indent)}-\s+",
        rest[1:],
    )
    insert_at = start + (1 + next_peer.start() if next_peer else len(rest))
    # Prefer inserting just before next peer; if none, at end of file.
    # Walk back to end of last non-empty line of this container.
    chunk = body[start:insert_at].rstrip("\n")
    snippet = _PROBE_SNIPPET.format(indent=field_indent, port=port)
    if not chunk.endswith("\n"):
        chunk += "\n"
    new_chunk = chunk + snippet
    return body[:start] + new_chunk + body[insert_at:]


def verify_workload_replicas(files: list[dict[str, Any]], *, minimum: int = 2) -> tuple[bool, str]:
    for entry in files:
        path = str(entry.get("target_path") or entry.get("path") or "")
        content = str(entry.get("content") or "")
        if replicas_at_least(content, minimum=minimum):
            return True, f"replicas>={minimum} in {path or 'staged workload'}"
    return False, f"no Deployment/Rollout with replicas>={minimum} in staged files"


def verify_workload_probes(files: list[dict[str, Any]]) -> tuple[bool, str]:
    for entry in files:
        path = str(entry.get("target_path") or entry.get("path") or "")
        content = str(entry.get("content") or "")
        if has_health_probes(content):
            return True, f"livenessProbe+readinessProbe in {path or 'staged workload'}"
    return False, "no Deployment/Rollout with livenessProbe+readinessProbe in staged files"
