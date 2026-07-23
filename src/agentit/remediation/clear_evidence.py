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
# Tekton Task that actually runs cosign sign/attest (not SLSA theater).
COSIGN_SIGN_TASK = "cosign_sign_task"
# App-repo CycloneDX/SPDX artifact — clears compliance ``sbom`` finding.
SBOM_FILE = "sbom_file"
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
    """True when a Dockerfile/Containerfile has FROM lines and no ``:latest``.

    When ``base_content`` is present on a staged file (delivery enrichment
    after fetching the existing Dockerfile), also refuse destructive
    rewrites that gut the file into a short stub (#165 / migration #163
    quality bar).
    """
    from agentit.remediation.source_patches import is_destructive_dockerfile_rewrite

    staged = _staged_map(files)
    base_by_path = {
        _file_path(f): f.get("base_content")
        for f in files
        if _file_path(f) and f.get("base_content")
    }
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
        base = base_by_path.get(path)
        if base:
            destructive, reason = is_destructive_dockerfile_rewrite(base, content)
            if destructive:
                return False, f"{path}: destructive rewrite refused — {reason}"
        # Refuse pin-only markers that never got enriched (would overwrite
        # a real file with a 2-line stub if committed).
        if "agentit-pin-only" in content and not base:
            return False, (
                f"{path}: pin-only marker not enriched with existing file — "
                "refusing theater stub"
            )
    return True, f"pinned base image in {candidates[0][0]} (no :latest)"


_AUDIT_THEATER_MARKERS = (
    "theater stub",
    "intentionally not wired",
)
# Real app-audit-logging patches emit structured records; refuse tiny stubs
# that only define a logger wrapper (pinky #12 class).
_AUDIT_STRUCTURE_MARKERS = (
    '"type": "audit"',
    '"type":"audit"',
    'type: "audit"',
    "json.dumps",
    "json.Marshal",
    "JSON.stringify",
    "AuditEvent",
)


def _audit_module_is_theater(content: str) -> str | None:
    """Return refuse reason if audit module content is a theater stub."""
    lowered = (content or "").lower()
    for marker in _AUDIT_THEATER_MARKERS:
        if marker in lowered:
            return f"theater stub marker ({marker!r}) — refuse Scan PR"
    lines = [ln for ln in (content or "").splitlines() if ln.strip()]
    has_structure = any(m.lower() in lowered for m in _AUDIT_STRUCTURE_MARKERS)
    if len(lines) < 12 and not has_structure:
        return (
            "audit module too thin / unstructured "
            f"({len(lines)} non-empty lines, no structured audit record) — "
            "refuse theater stub"
        )
    if not has_structure:
        return (
            "audit module lacks structured audit record markers "
            "(json / type=audit / AuditEvent) — refuse theater stub"
        )
    return None


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
    for path in (packaged or audit_paths):
        theater_reason = _audit_module_is_theater(staged.get(path, ""))
        if theater_reason:
            return False, f"{path}: {theater_reason}"
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


_TARGET_METADATA_NONE = re.compile(r"target_metadata\s*=\s*None\b")
_TARGET_METADATA_WIRED = re.compile(r"target_metadata\s*=\s*(?!None\b)\S+")
_UPGRADE_DEF = re.compile(r"^\s*def\s+upgrade\s*\(", re.MULTILINE)


def verify_migration_tooling(files: list[dict]) -> tuple[bool, str]:
    """Accept real migrators; refuse ``target_metadata = None`` theater stubs.

    A greenfield Alembic PR must wire URL/metadata **or** ship a revision
    with ``upgrade()``. Config-only / empty-env scaffolds do not clear the
    finding and must not pass pre-open simulation (see closed AgentIT #157).
    """
    staged = _staged_map(files)
    has_alembic_ini = False
    has_theater_env = False
    has_wired_metadata = False
    has_url_wiring = False
    has_revision = False
    has_sql_migration = False
    has_other_tool = False

    for path, content in staged.items():
        low = path.lower().replace("\\", "/")
        body = content or ""
        if low.endswith("alembic.ini") or low.endswith("/alembic.ini"):
            has_alembic_ini = True
        if any(m in low for m in ("flyway.conf", "flyway.toml", "liquibase")):
            has_other_tool = True
        if ("/goose/" in low or low.startswith("goose/")) and low.endswith(".sql"):
            if body.strip():
                has_sql_migration = True
        if "db/migrate" in low and body.strip():
            has_sql_migration = True
        if (
            ("/migrations/" in low or low.startswith("migrations/"))
            and low.endswith(".sql")
            and len(body.strip()) > 20
        ):
            has_sql_migration = True
        if low.endswith("env.py") and "alembic" in body.lower():
            if _TARGET_METADATA_NONE.search(body):
                has_theater_env = True
            if _TARGET_METADATA_WIRED.search(body):
                has_wired_metadata = True
            if "os.environ" in body and any(
                key in body for key in ("DATABASE_URL", "SQLALCHEMY_URL", "AGENTIT_DB_DSN")
            ):
                has_url_wiring = True
        if (
            ("/versions/" in low or "/migrations/" in low)
            and low.endswith(".py")
            and _UPGRADE_DEF.search(body)
        ):
            has_revision = True

    if has_other_tool:
        return True, "Flyway/Liquibase migration config in staged files"
    if has_sql_migration:
        return True, "versioned SQL migration file(s) in staged files"
    if has_alembic_ini and has_wired_metadata:
        return True, "alembic.ini + env.py with wired target_metadata"
    if has_alembic_ini and has_revision:
        detail = "alembic.ini + revision with upgrade()"
        if has_url_wiring:
            detail += " + env URL wiring"
        return True, detail
    if has_theater_env:
        return False, (
            "alembic env sets target_metadata = None without a real revision "
            "(theater stub — refuse Scan PR)"
        )
    if has_alembic_ini:
        return False, "alembic.ini without wired metadata or upgrade() revision"
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



def verify_sbom_file(files: list[dict]) -> tuple[bool, str]:
    """True when staged files include a CycloneDX/SPDX SBOM artifact.

    Matches ComplianceAnalyzer / ``sbom-exists`` filename globs (``*sbom*`` /
    ``*bom*``) but refuses empty ``{}`` theater — require bomFormat or SPDX
    markers so Scan PRs clear honestly (founder bar: merge clears finding).
    """
    staged = _staged_map(files)
    named: list[tuple[str, str]] = []
    for path, content in staged.items():
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if "sbom" in name or "bom" in name:
            named.append((path, content))
    if not named:
        return False, "no *sbom*/*bom* artifact in staged files"
    for path, content in named:
        body = (content or "").strip()
        if not body:
            continue
        low = body.lower()
        if "bomformat" in low and "cyclonedx" in low:
            return True, f"CycloneDX SBOM in {path}"
        if "spdxversion" in low or '"spdxid"' in low or "spdx-license-id" in low:
            return True, f"SPDX SBOM in {path}"
        if re.search(r"^\s*kind:\s*Task\s*$", body, re.I | re.M):
            return False, (
                f"{path}: Tekton Task does not clear app-repo SBOM finding "
                "(need CycloneDX/SPDX artifact — refuse sbom-task theater)"
            )
    return False, (
        f"{named[0][0]}: sbom-named file without CycloneDX/SPDX content "
        "(refuse empty theater stub)"
    )


def verify_cosign_sign_task(files: list[dict]) -> tuple[bool, str]:
    """True when staged files include a Tekton Task that runs cosign sign/attest.

    Refuses empty Task stubs and SLSA L3 / hermetic / Konflux prose theater
    that never invokes ``cosign sign`` or ``cosign attest``.
    """
    staged = _staged_map(files)
    tasks: list[tuple[str, str]] = []
    for path, content in staged.items():
        if re.search(r"^\s*kind:\s*(Cluster)?Task\s*$", content, re.I | re.M):
            tasks.append((path, content))
    if not tasks:
        return False, "no Tekton Task/ClusterTask in staged files"
    theater_rx = re.compile(
        r"\b(?:slsa\s*l(?:evel)?\s*3|hermetic|konflux)\b", re.I,
    )
    cosign_cmd = re.compile(r"\bcosign\s+(sign|attest)\b", re.I)
    for path, content in tasks:
        if cosign_cmd.search(content):
            return True, f"cosign sign/attest Task in {path}"
        if theater_rx.search(content) and "cosign" not in content.lower():
            return False, (
                f"{path}: SLSA/hermetic/Konflux theater without cosign "
                "(refuse — need cosign sign or cosign attest)"
            )
    return False, (
        f"{tasks[0][0]}: Task without cosign sign/attest "
        "(refuse empty or non-signing Task theater)"
    )


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
    if evidence_kind == COSIGN_SIGN_TASK:
        return verify_cosign_sign_task(files)
    if evidence_kind == SBOM_FILE:
        return verify_sbom_file(files)
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
