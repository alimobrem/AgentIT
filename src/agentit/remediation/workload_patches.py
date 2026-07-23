"""Source patches for Deployment/Rollout replicas and health probes.

Assess ``replicas`` / ``health`` findings scan app-repo YAML. Clear-evidence
requires the same shapes in staged files — not a Kyverno mutate Policy alone.

A Helm-templated workload never has a literal ``replicas: <digit>`` line —
it references a values key (``replicas: {{ .Values.replicaCount }}``), so
none of the plain-text helpers below can find or safely patch it in place
(inserting a second, literal ``replicas:`` key next to the templated one
would corrupt the chart, not fix it). ``helm_templated_replicas_key`` /
``chart_root_for_template_path`` / ``patch_values_numeric_key`` exist so a
caller with real repo access (``source_patches.enrich_workload_files_from_repo``)
can detect that indirection and patch the chart's own ``values.yaml``
instead — the same "prove the file you're patching actually attaches to the
live workload" lesson ``self_managed_hpa.py`` / ``fleet_hpa.py`` already
apply to HPA ``scaleTargetRef``.
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
# ``replicas: {{ .Values.replicaCount }}`` (optionally ``{{- ... -}}``/quoted).
_HELM_REPLICAS_TEMPLATE_RE = re.compile(
    r"^[ \t]*replicas:\s*[\"']?\{\{-?\s*\.Values\.([A-Za-z0-9_.]+)\s*-?\}\}[\"']?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# A top-level (unindented) ``replicaCount: N`` / ``replicas: N`` style key in
# a chart's values.yaml — deliberately loose on the key name (charts vary),
# strict on "top-level" to avoid ambiguously patching/matching a nested key
# with the same leaf name under a different parent.
_VALUES_REPLICA_KEY_RE = re.compile(
    r"^(\w*replica\w*):\s*[\"']?(\d+)[\"']?",
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


def helm_templated_replicas_key(content: str) -> str | None:
    """The ``.Values.<key>`` a workload's ``replicas:`` line references, if any.

    ``None`` for a non-workload doc, a workload with a literal replicas
    count, or one with no ``replicas:`` line at all.
    """
    if not is_workload_manifest(content or ""):
        return None
    m = _HELM_REPLICAS_TEMPLATE_RE.search(content or "")
    return m.group(1) if m else None


def chart_root_for_template_path(path: str) -> str | None:
    """``chart/templates/deployment.yaml`` -> ``"chart"``; ``"templates/x.yaml"``
    -> ``""`` (top-level chart). ``None`` when ``path`` has no ``templates/``
    segment at all — not a Helm chart template path."""
    parts = (path or "").replace("\\", "/").split("/")
    if "templates" not in parts:
        return None
    idx = parts.index("templates")
    return "/".join(parts[:idx])


def values_yaml_path_for_chart(chart_root: str) -> str:
    return f"{chart_root}/values.yaml" if chart_root else "values.yaml"


def values_yaml_replicas_at_least(content: str, minimum: int = 2) -> bool:
    """True when a chart's values.yaml declares a top-level replica-count
    key (``replicaCount`` and similar) >= ``minimum``. The literal
    Deployment/Rollout YAML never carries the number itself once a chart
    templates ``replicas:`` via ``{{ .Values.<key> }}``."""
    for m in _VALUES_REPLICA_KEY_RE.finditer(content or ""):
        try:
            if int(m.group(2)) >= minimum:
                return True
        except ValueError:
            continue
    return False


def patch_values_numeric_key(content: str, key: str, *, minimum: int = 2) -> str | None:
    """Bump a top-level ``key: <int>`` line in ``values.yaml`` content.

    Returns the input unchanged when already ``>= minimum``, or ``None``
    when ``key`` is nested (contains ``.``) or is not a plain numeric
    top-level scalar in ``content`` — refuse rather than guess at an
    ambiguous or absent chart key.
    """
    if not key or "." in key:
        return None
    pattern = re.compile(
        rf"^({re.escape(key)}):([ \t]*)[\"']?(\d+)[\"']?([ \t]*#.*)?[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(content or "")
    if not m:
        return None
    try:
        current = int(m.group(3))
    except ValueError:
        return None
    if current >= minimum:
        return content or ""
    sep = m.group(2) or " "
    comment = m.group(4) or ""
    new_line = f"{m.group(1)}:{sep}{minimum}{comment}"
    body = content or ""
    return body[: m.start()] + new_line + body[m.end():]


def verify_workload_replicas(files: list[dict[str, Any]], *, minimum: int = 2) -> tuple[bool, str]:
    for entry in files:
        path = str(entry.get("target_path") or entry.get("path") or "")
        content = str(entry.get("content") or "")
        if replicas_at_least(content, minimum=minimum):
            return True, f"replicas>={minimum} in {path or 'staged workload'}"
        if path.endswith("values.yaml") and values_yaml_replicas_at_least(
            content, minimum=minimum,
        ):
            return True, f"replica count>={minimum} in {path} (chart values)"
    return False, f"no Deployment/Rollout with replicas>={minimum} in staged files"


def verify_workload_probes(files: list[dict[str, Any]]) -> tuple[bool, str]:
    for entry in files:
        path = str(entry.get("target_path") or entry.get("path") or "")
        content = str(entry.get("content") or "")
        if has_health_probes(content):
            return True, f"livenessProbe+readinessProbe in {path or 'staged workload'}"
    return False, "no Deployment/Rollout with livenessProbe+readinessProbe in staged files"
