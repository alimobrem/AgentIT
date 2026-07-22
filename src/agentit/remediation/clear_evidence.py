"""Pre-open clear-evidence simulation for solution contracts.

Before Scan opens a PR, verify the *staged* files would actually clear each
contracted finding — not by keyword hope, but by the same shapes analyzers
and live gates use after merge (Dockerfile pin, audit import/usage, HPA
scaleTargetRef, ResourceQuota/LimitRange, …).

Founder bar: MERGE clears the finding. If simulation fails, refuse the PR
and say why.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from agentit.remediation.audit_wire import has_audit_usage

logger = logging.getLogger(__name__)

# Evidence kinds declared on SolutionContract.evidence_kind
DOCKERFILE_PIN = "dockerfile_pin"
AUDIT_WIRED = "audit_wired"
RUNTIME_PIN = "runtime_pin"
MIGRATION_TOOLING = "migration_tooling"
HELM_CHART = "helm_chart"
HPA_TARGET = "hpa_target"
QUOTA_MANIFEST = "quota_manifest"
CLUSTER_KIND = "cluster_kind"
RESOURCE_LIMITS = "resource_limits"
DETECT_ONLY = "detect_only"
# Non-skill sentinel (patch_base_image) — same pin check as dockerfile.
BASE_IMAGE_PIN = "base_image_pin"

_FROM_LATEST = re.compile(r"^\s*FROM\s+\S+:latest\b", re.IGNORECASE | re.MULTILINE)
_FROM_LINE = re.compile(r"^\s*FROM\s+\S+", re.IGNORECASE | re.MULTILINE)
_HPA_KIND = re.compile(r"^\s*kind:\s*HorizontalPodAutoscaler\s*$", re.IGNORECASE | re.MULTILINE)
_SCALE_REF = re.compile(
    r"scaleTargetRef:\s*\n(?:[ \t]+[^\n]+\n)*?[ \t]+kind:\s*(\w+)\s*\n"
    r"(?:[ \t]+[^\n]+\n)*?[ \t]+name:\s*[\"']?([^\s\"']+)",
    re.IGNORECASE,
)
_SCALE_REF_FLAT = re.compile(
    r"scaleTargetRef:.*?kind:\s*(\w+).*?name:\s*[\"']?([^\s\"']+)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class EvidenceResult:
    """One finding's pre-open evidence check."""

    category: str
    ok: bool
    reason: str
    evidence_kind: str


def _file_path(entry: dict) -> str:
    return str(entry.get("target_path") or entry.get("path") or "")


def _file_content(entry: dict) -> str:
    return str(entry.get("content") or "")


def _staged_map(files: list[dict]) -> dict[str, str]:
    """path → content (prefer target_path; last write wins)."""
    out: dict[str, str] = {}
    for f in files:
        path = _file_path(f)
        if path:
            out[path] = _file_content(f)
    return out


def verify_dockerfile_pin(files: list[dict]) -> tuple[bool, str]:
    """True when a Dockerfile/Containerfile has FROM lines and no ``:latest``."""
    staged = _staged_map(files)
    candidates = [
        (p, c) for p, c in staged.items()
        if "dockerfile" in p.lower() or "containerfile" in p.lower()
        or p.lower().endswith("dockerfile") or p.lower().endswith("containerfile")
    ]
    if not candidates:
        # Also accept content that looks like a Dockerfile even under odd paths
        for p, c in staged.items():
            if _FROM_LINE.search(c) and ("USER" in c or "HEALTHCHECK" in c or "WORKDIR" in c):
                candidates.append((p, c))
    if not candidates:
        return False, "no Dockerfile/Containerfile in staged files"
    for path, content in candidates:
        if not _FROM_LINE.search(content):
            return False, f"{path}: missing FROM line"
        if _FROM_LATEST.search(content):
            return False, f"{path}: still uses :latest on a FROM line"
    return True, f"pinned base image in {candidates[0][0]} (no :latest)"


def verify_audit_wired(files: list[dict]) -> tuple[bool, str]:
    """True when an audit module exists and a package entry imports/uses it."""
    staged = _staged_map(files)
    audit_paths = [
        p for p in staged
        if p.rstrip("/").endswith(("audit.py", "audit.ts", "audit.js", "audit.go"))
    ]
    if not audit_paths:
        return False, "no audit module (audit.py/ts/js/go) in staged files"
    # Prefer package-path modules (apps/.../audit.py), not orphan root.
    packaged = [p for p in audit_paths if "/" in p and not p.startswith("audit.")]
    if not packaged and all("/" not in p for p in audit_paths):
        return False, (
            f"audit module at repo root only ({audit_paths[0]}) — "
            "must live in the app package with an import/call site"
        )
    # Usage evidence: any non-audit staged file that imports/calls audit.
    for path, content in staged.items():
        if path in audit_paths:
            continue
        if has_audit_usage(content) or "agentit_audit_middleware" in content:
            return True, f"audit module + usage in {path}"
    # Module itself may include helpers; still require a separate callsite file.
    return False, (
        f"audit module present ({audit_paths[0]}) but no import/usage "
        "call site in staged files"
    )


def verify_runtime_pin(files: list[dict]) -> tuple[bool, str]:
    staged = _staged_map(files)
    pins = (
        ".node-version", ".python-version", ".nvmrc", ".tool-versions",
        "runtime.txt",
    )
    for path, content in staged.items():
        base = path.rsplit("/", 1)[-1]
        if base in pins or base.endswith("version"):
            text = content.strip()
            if text and any(ch.isdigit() for ch in text):
                return True, f"runtime pin {path}={text.splitlines()[0][:40]}"
    return False, "no .node-version / .python-version (or similar) pin with a version"


def verify_migration_tooling(files: list[dict]) -> tuple[bool, str]:
    staged = _staged_map(files)
    markers = (
        "alembic.ini", "flyway.conf", "flyway.toml", "liquibase",
        "db/migrate", "goose/",
    )
    for path, content in staged.items():
        low = path.lower()
        if any(m in low for m in markers):
            if "alembic.ini" in low or "flyway" in low or "liquibase" in low:
                return True, f"migration config at {path}"
            if content.strip():
                return True, f"migration path {path}"
        if low.endswith("env.py") and "alembic" in content.lower():
            return True, f"alembic env at {path}"
    return False, "no Alembic/Flyway/Liquibase/goose migration tooling in staged files"


def verify_helm_chart(files: list[dict]) -> tuple[bool, str]:
    staged = _staged_map(files)
    chart_yaml = next(
        (p for p in staged if p.endswith("Chart.yaml") or p.endswith("Chart.yml")),
        None,
    )
    if not chart_yaml:
        return False, "no Chart.yaml in staged files"
    templates = [
        p for p, c in staged.items()
        if "/templates/" in p.replace("\\", "/")
        and ("apiVersion:" in c and "kind:" in c)
    ]
    if not templates:
        return False, f"{chart_yaml} present but no templates with apiVersion/kind"
    return True, f"Helm chart {chart_yaml} + {len(templates)} template(s)"


def _parse_hpa_targets(content: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    if not _HPA_KIND.search(content) and "HorizontalPodAutoscaler" not in content:
        return targets
    for rx in (_SCALE_REF, _SCALE_REF_FLAT):
        for m in rx.finditer(content):
            kind, name = m.group(1), m.group(2)
            if kind and name:
                targets.append((kind, name))
    return targets


def verify_hpa_target(
    files: list[dict],
    *,
    live_workloads: list[dict[str, str]] | None = None,
) -> tuple[bool, str]:
    """HPA must declare Deployment/Rollout scaleTargetRef; optionally resolve live."""
    staged = _staged_map(files)
    found: list[tuple[str, str, str]] = []
    for path, content in staged.items():
        for kind, name in _parse_hpa_targets(content):
            found.append((path, kind, name))
    if not found:
        return False, "no HPA with scaleTargetRef kind/name in staged files"
    for path, kind, name in found:
        if kind not in ("Deployment", "Rollout"):
            return False, f"{path}: scaleTargetRef.kind={kind} (need Deployment|Rollout)"
    if live_workloads is not None:
        live_keys = {
            (str(w.get("kind") or ""), str(w.get("name") or ""))
            for w in live_workloads
        }
        for path, kind, name in found:
            if (kind, name) not in live_keys:
                live_desc = ", ".join(
                    f"{w.get('kind')}/{w.get('name')}" for w in live_workloads[:8]
                ) or "(none)"
                return False, (
                    f"{path}: scaleTargetRef {kind}/{name} not in live workloads "
                    f"[{live_desc}]"
                )
        return True, f"HPA scaleTargetRef resolves live ({found[0][1]}/{found[0][2]})"
    return True, f"HPA scaleTargetRef {found[0][1]}/{found[0][2]} (shape ok)"


def verify_quota_manifest(files: list[dict]) -> tuple[bool, str]:
    staged = _staged_map(files)
    for path, content in staged.items():
        if re.search(r"^\s*kind:\s*ResourceQuota\s*$", content, re.I | re.M):
            return True, f"ResourceQuota in {path}"
        if re.search(r"^\s*kind:\s*LimitRange\s*$", content, re.I | re.M):
            return True, f"LimitRange in {path}"
    return False, "no ResourceQuota or LimitRange in staged files"


def verify_cluster_kind(files: list[dict], kinds: frozenset[str]) -> tuple[bool, str]:
    if not kinds:
        return False, "contract missing evidence_params (expected K8s kinds)"
    staged = _staged_map(files)
    for path, content in staged.items():
        for kind in kinds:
            if re.search(rf"^\s*kind:\s*{re.escape(kind)}\s*$", content, re.I | re.M):
                return True, f"kind:{kind} in {path}"
    return False, f"none of kinds {sorted(kinds)} found in staged files"


def verify_resource_limits(files: list[dict]) -> tuple[bool, str]:
    staged = _staged_map(files)
    for path, content in staged.items():
        if re.search(r"^\s*kind:\s*LimitRange\s*$", content, re.I | re.M):
            return True, f"LimitRange in {path}"
        if re.search(r"resources:\s*\n(?:.*\n)*?\s+(requests|limits)\s*:", content):
            return True, f"container resources requests/limits in {path}"
    return False, "no container resources requests/limits or LimitRange in staged files"


def verify_evidence(
    evidence_kind: str,
    files: list[dict],
    *,
    evidence_params: frozenset[str] | None = None,
    live_workloads: list[dict[str, str]] | None = None,
) -> tuple[bool, str]:
    """Dispatch to the verifier for ``evidence_kind``."""
    params = evidence_params or frozenset()
    if evidence_kind == DETECT_ONLY:
        return False, "detect_only / no_auto_pr — Scan must not open a PR"
    if evidence_kind in (DOCKERFILE_PIN, BASE_IMAGE_PIN):
        return verify_dockerfile_pin(files)
    if evidence_kind == AUDIT_WIRED:
        return verify_audit_wired(files)
    if evidence_kind == RUNTIME_PIN:
        return verify_runtime_pin(files)
    if evidence_kind == MIGRATION_TOOLING:
        return verify_migration_tooling(files)
    if evidence_kind == HELM_CHART:
        return verify_helm_chart(files)
    if evidence_kind == HPA_TARGET:
        return verify_hpa_target(files, live_workloads=live_workloads)
    if evidence_kind == QUOTA_MANIFEST:
        return verify_quota_manifest(files)
    if evidence_kind == CLUSTER_KIND:
        return verify_cluster_kind(files, params)
    if evidence_kind == RESOURCE_LIMITS:
        return verify_resource_limits(files)
    return False, f"unknown evidence_kind {evidence_kind!r}"


def simulate_finding_clearance(
    files: list[dict],
    target_findings: list[tuple[str, str]],
    *,
    live_workloads: list[dict[str, str]] | None = None,
    self_managed: bool | None = None,
) -> list[EvidenceResult]:
    """Simulate clear-evidence for each remediable target finding.

    ``self_managed`` is accepted for call-site honesty / future path checks;
    verifiers today are content-shaped (fleet vs chart layout is enforced by
    delivery routing + strip_wrong_layer_companions).
    """
    del self_managed  # layout enforced elsewhere; keep param for API stability
    from agentit.remediation.registry import allows_auto_pr, contract_for

    results: list[EvidenceResult] = []
    for cat, _desc in target_findings:
        if not cat:
            continue
        contract = contract_for(cat)
        if contract is None:
            results.append(EvidenceResult(
                category=cat, ok=False, evidence_kind="",
                reason="uncontracted finding — refusing Scan PR (add SOLUTION_CONTRACT)",
            ))
            continue
        if not allows_auto_pr(cat):
            results.append(EvidenceResult(
                category=cat, ok=False, evidence_kind=contract.evidence_kind,
                reason=f"detect_only / no_auto_pr — {contract.clear_evidence}",
            ))
            continue
        # Only files that match this finding's clearing skill participate.
        skill = contract.skill_name
        relevant = [
            f for f in files
            if not skill
            or str(f.get("skill_name") or "") == skill
            or skill.replace("-", "_") in _file_path(f).replace("-", "_").lower()
        ]
        check_files = relevant or list(files)
        ok, reason = verify_evidence(
            contract.evidence_kind,
            check_files,
            evidence_params=contract.evidence_params,
            live_workloads=live_workloads if contract.evidence_kind == HPA_TARGET else None,
        )
        results.append(EvidenceResult(
            category=cat, ok=ok, evidence_kind=contract.evidence_kind, reason=reason,
        ))
    return results


def simulation_gate(
    files: list[dict],
    target_findings: list[tuple[str, str]],
    *,
    live_workloads: list[dict[str, str]] | None = None,
    self_managed: bool | None = None,
) -> tuple[bool, str, list[EvidenceResult]]:
    """Return (allowed, refuse_reason, per-finding results).

    Allowed only when every remediable finding in ``target_findings`` passes
    evidence simulation. Empty remediable set → caller should use finding_gate.
    """
    from agentit.remediation.registry import remediable_findings

    remediable = remediable_findings(target_findings)
    if not remediable:
        return False, "no remediable findings for clear-evidence simulation", []

    results = simulate_finding_clearance(
        files, remediable,
        live_workloads=live_workloads,
        self_managed=self_managed,
    )
    failed = [r for r in results if not r.ok]
    if failed:
        parts = [f"`{r.category}` ({r.evidence_kind or '?'}): {r.reason}" for r in failed]
        reason = (
            "Clear-evidence simulation failed — refusing PR (MERGE would not "
            "clear the finding): " + "; ".join(parts)
        )
        return False, reason, results
    summary = "; ".join(f"`{r.category}`: {r.reason}" for r in results)
    return True, summary, results


def contract_lines_for_portal(target_findings: list[Any]) -> list[str]:
    """Short honesty lines for PR cards (Clears X by Y)."""
    from agentit.remediation.registry import expected_clear_lines

    normalized: list[tuple[str, str]] = []
    for item in target_findings or []:
        if isinstance(item, (list, tuple)) and item:
            normalized.append((str(item[0]), str(item[1]) if len(item) > 1 else ""))
        elif isinstance(item, dict) and item.get("category"):
            normalized.append((str(item["category"]), str(item.get("description") or "")))
    return expected_clear_lines(normalized)
