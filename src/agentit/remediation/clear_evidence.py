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
# Tekton image-scan Task with a real scanner step (not empty / :latest theater).
IMAGE_SCAN_TASK = "image_scan_task"
# Grafana dashboard ConfigMap with label + non-empty panels JSON.
GRAFANA_DASHBOARD = "grafana_dashboard"
# PDB / ServiceMonitor whose selector matches live Services/workloads.
SELECTOR_TARGET = "selector_target"
# Argo CD Application with real source.repoURL + path/chart.
ARGOCD_APPLICATION = "argocd_application"
# App-repo CycloneDX/SPDX artifact — legacy / fallback only (not primary clear).
SBOM_FILE = "sbom_file"
# CI generates SBOM (GHA anchore/sbom-action / syft, or Tekton Pipeline wire).
SBOM_CI = "sbom_ci"
# Non-skill sentinel (patch_base_image) — same pin check as dockerfile.
BASE_IMAGE_PIN = "base_image_pin"

_FROM_LATEST = re.compile(r"^\s*FROM\s+\S+:latest\b", re.IGNORECASE | re.MULTILINE)
_FROM_LINE = re.compile(r"^\s*FROM\s+\S+", re.IGNORECASE | re.MULTILINE)
# Analyzer descriptions end with `` in {rel_path}`` / `` in {df.name}``.
_FINDING_FILE_PATH = re.compile(
    r"\bin\s+((?:[\w./-]+/)?(?:[Dd]ockerfile|[Cc]ontainerfile)[\w./-]*)\s*$",
)
_UBI_FROM = re.compile(
    r"^\s*FROM\s+\S*(?:ubi|redhat|registry\.access\.redhat)\S*",
    re.IGNORECASE | re.MULTILINE,
)
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


def _is_containerfile_path(path: str) -> bool:
    p = path.lower()
    return (
        "dockerfile" in p
        or "containerfile" in p
        or p.endswith("dockerfile")
        or p.endswith("containerfile")
    )


def _container_finding_subtype(description: str) -> str:
    """Classify a container finding so dockerfile_pin cannot overclaim.

    Returns one of: ``healthcheck``, ``user``, ``ubi``, ``latest``, ``pin``.
    """
    d = (description or "").lower()
    if "healthcheck" in d:
        return "healthcheck"
    if "runs as root" in d or "no user directive" in d:
        return "user"
    if "not ubi" in d or "universal base image" in d:
        return "ubi"
    if ":latest" in d or "latest tag" in d:
        return "latest"
    return "pin"


def _finding_containerfile_path(description: str) -> str | None:
    """Extract Dockerfile/Containerfile path from an analyzer description."""
    m = _FINDING_FILE_PATH.search(description or "")
    return m.group(1) if m else None


def _resolve_staged_containerfile(
    staged: dict[str, str],
    target: str,
) -> tuple[str, str] | None:
    """Match ``target`` to a staged path (exact, casefold, or basename)."""
    if not target:
        return None
    if target in staged:
        return target, staged[target]
    target_cf = target.casefold()
    for path, content in staged.items():
        if path.casefold() == target_cf:
            return path, content
    base = target.rsplit("/", 1)[-1].casefold()
    basename_hits = [
        (p, c) for p, c in staged.items()
        if p.rsplit("/", 1)[-1].casefold() == base
    ]
    if len(basename_hits) == 1:
        return basename_hits[0]
    return None


def _check_containerfile_pin_content(
    path: str,
    content: str,
    *,
    base_content: str | None = None,
) -> tuple[bool, str]:
    """Pin / rewrite checks for one staged Dockerfile/Containerfile."""
    from agentit.remediation.source_patches import is_destructive_dockerfile_rewrite

    if not _FROM_LINE.search(content):
        return False, f"{path}: missing FROM line"
    if _FROM_LATEST.search(content):
        return False, f"{path}: still uses :latest on a FROM line"
    if base_content:
        destructive, reason = is_destructive_dockerfile_rewrite(base_content, content)
        if destructive:
            return False, f"{path}: destructive rewrite refused — {reason}"
    if "agentit-pin-only" in content and not base_content:
        return False, (
            f"{path}: pin-only marker not enriched with existing file — "
            "refusing theater stub"
        )
    return True, f"pinned base image in {path} (no :latest)"


def verify_dockerfile_pin(
    files: list[dict],
    *,
    finding_description: str = "",
) -> tuple[bool, str]:
    """True when staged Dockerfiles clear the *targeted* container finding.

    Path-bound: when the finding names a Dockerfile/Containerfile, that file
    must be staged and pinned — pinning an unrelated Dockerfile must not clear
    ``:latest`` findings on Dockerfile.deps / .fast (pulse-agent#2 class).

    Category mismatch: HEALTHCHECK / USER(root) findings cannot clear via
    ``dockerfile_pin`` alone. Non-UBI findings require a UBI/Red Hat FROM on
    the finding's file (pin-only of a non-UBI base is not enough).

    When ``base_content`` is present on a staged file (delivery enrichment
    after fetching the existing Dockerfile), also refuse destructive
    rewrites that gut the file into a short stub (#165 / migration #163
    quality bar).
    """
    subtype = _container_finding_subtype(finding_description)
    if subtype == "healthcheck":
        return False, (
            "HEALTHCHECK finding cannot clear via dockerfile_pin — "
            "need HEALTHCHECK directive (finding/patch mismatch)"
        )
    if subtype == "user":
        return False, (
            "USER/root finding cannot clear via dockerfile_pin — "
            "need USER directive (finding/patch mismatch)"
        )

    staged = _staged_map(files)
    base_by_path = {
        _file_path(f): f.get("base_content")
        for f in files
        if _file_path(f) and f.get("base_content")
    }
    candidates = [
        (p, c) for p, c in staged.items() if _is_containerfile_path(p)
    ]
    if not candidates:
        for p, c in staged.items():
            if _FROM_LINE.search(c) and ("USER" in c or "HEALTHCHECK" in c or "WORKDIR" in c):
                candidates.append((p, c))
    if not candidates:
        return False, "no Dockerfile/Containerfile in staged files"

    target = _finding_containerfile_path(finding_description)
    if target:
        resolved = _resolve_staged_containerfile(staged, target)
        if resolved is None:
            return False, (
                f"{target}: not staged — cannot clear finding for that path "
                f"(refusing overclaim from unrelated Dockerfile pin)"
            )
        path, content = resolved
        base = base_by_path.get(path) or base_by_path.get(target)
        ok, reason = _check_containerfile_pin_content(
            path, content, base_content=base if isinstance(base, str) else None,
        )
        if not ok:
            return False, reason
        if subtype == "ubi" and not _UBI_FROM.search(content):
            return False, (
                f"{path}: non-UBI finding cannot clear via pin alone — "
                "FROM must use UBI/Red Hat base (finding/patch mismatch)"
            )
        return True, reason

    # No path in description (legacy / generic): every staged containerfile
    # must be pinned — still refuse UBI/HEALTHCHECK subtypes above.
    if subtype == "ubi":
        return False, (
            "non-UBI finding cannot clear via dockerfile_pin alone — "
            "need UBI/Red Hat FROM on the finding's Dockerfile "
            "(finding/patch mismatch)"
        )
    for path, content in candidates:
        base = base_by_path.get(path)
        ok, reason = _check_containerfile_pin_content(
            path, content, base_content=base if isinstance(base, str) else None,
        )
        if not ok:
            return False, reason
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
_UPGRADE_BODY = re.compile(
    r"def\s+upgrade\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:\s*\n"
    r"((?:[ \t]+.*\n|\s*\n)*)",
    re.MULTILINE,
)
_DDL_SQL = re.compile(
    r"\b(CREATE|ALTER|DROP|TRUNCATE|RENAME)\s+"
    r"(TABLE|INDEX|TYPE|SCHEMA|VIEW|SEQUENCE|COLUMN|CONSTRAINT|MATERIALIZED)\b",
    re.IGNORECASE,
)
_SELECT_ONE = re.compile(r"\bSELECT\s+1\b", re.IGNORECASE)
_COMMENT_ONLY_EXECUTE = re.compile(
    r"""op\.execute\s*\(\s*(?:f?['"])\s*--""",
    re.IGNORECASE,
)


def _strip_sql_noise(body: str) -> str:
    """Drop SQL/Python comments and blank lines for shallow-stub detection."""
    kept: list[str] = []
    for ln in (body or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("--") or s.startswith("#"):
            continue
        if s.startswith('"""') or s.startswith("'''"):
            continue
        kept.append(s)
    return "\n".join(kept)


def _body_has_real_ddl(body: str) -> bool:
    """True when body contains schema DDL (SQL keywords or Alembic create_*)."""
    if not (body or "").strip():
        return False
    if _DDL_SQL.search(body):
        return True
    return bool(re.search(
        r"\bop\.(create_table|drop_table|add_column|drop_column|alter_column|"
        r"create_index|drop_index)\s*\(",
        body,
        re.IGNORECASE,
    ))


def _alembic_upgrade_has_real_ddl(content: str) -> tuple[bool, str]:
    """Return (ok, refuse_reason) for an Alembic revision's upgrade()."""
    if not _UPGRADE_DEF.search(content or ""):
        return False, "revision missing upgrade()"
    m = _UPGRADE_BODY.search(content or "")
    body = m.group(1) if m else ""
    stripped = _strip_sql_noise(body)
    if not stripped or re.fullmatch(r"pass(\b.*)?", stripped, re.I | re.S):
        return False, "empty upgrade()/pass — refuse theater (need real DDL)"
    if _COMMENT_ONLY_EXECUTE.search(body) and not _DDL_SQL.search(body):
        return False, "comment-only op.execute — refuse theater (need real DDL)"
    if _SELECT_ONE.search(stripped) and not _body_has_real_ddl(body):
        return False, "SELECT 1 stub — refuse theater (need real DDL)"
    if not _body_has_real_ddl(body):
        return False, "upgrade() without real DDL — refuse shallow migration"
    return True, "revision upgrade() has real DDL"


def _sql_file_has_real_ddl(content: str) -> tuple[bool, str]:
    stripped = _strip_sql_noise(content or "")
    if not stripped:
        return False, "empty SQL migration"
    if _SELECT_ONE.search(stripped) and not _DDL_SQL.search(content or ""):
        return False, "SELECT 1 stub — refuse theater (need real DDL)"
    if not _DDL_SQL.search(content or ""):
        return False, "SQL migration without CREATE/ALTER/DROP DDL"
    return True, "versioned SQL with real DDL"


def verify_migration_tooling(files: list[dict]) -> tuple[bool, str]:
    """Accept real migrators; refuse shallow ``SELECT 1`` / empty upgrade theater.

    A greenfield Alembic PR must ship a revision whose ``upgrade()`` runs
    real DDL (or wired MetaData). Config-only scaffolds, ``pass``,
    comment-only ``op.execute``, and ``SELECT 1`` stubs do not clear the
    finding (see #157 / skills-audit shallow PRs). Skip the PR when the
    analyzer already sees hand-rolled store DDL — do not open theater.
    """
    staged = _staged_map(files)
    has_alembic_ini = False
    has_theater_env = False
    has_wired_metadata = False
    has_url_wiring = False
    revision_ok = False
    revision_refuse: str | None = None
    sql_ok = False
    sql_refuse: str | None = None
    has_other_tool = False

    for path, content in staged.items():
        low = path.lower().replace("\\", "/")
        body = content or ""
        if low.endswith("alembic.ini") or low.endswith("/alembic.ini"):
            has_alembic_ini = True
        if any(m in low for m in ("flyway.conf", "flyway.toml", "liquibase")):
            has_other_tool = True
        is_sql_mig = (
            (("/goose/" in low or low.startswith("goose/")) and low.endswith(".sql"))
            or ("db/migrate" in low and low.endswith(".sql"))
            or (
                ("/migrations/" in low or low.startswith("migrations/"))
                and low.endswith(".sql")
            )
        )
        if is_sql_mig:
            ok_sql, why = _sql_file_has_real_ddl(body)
            if ok_sql:
                sql_ok = True
            else:
                sql_refuse = f"{path}: {why}"
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
            ok_rev, why = _alembic_upgrade_has_real_ddl(body)
            if ok_rev:
                revision_ok = True
            else:
                revision_refuse = f"{path}: {why}"

    if has_other_tool:
        return True, "Flyway/Liquibase migration config in staged files"
    if sql_ok:
        return True, "versioned SQL migration file(s) with real DDL"
    if sql_refuse and not revision_ok and not has_wired_metadata:
        return False, sql_refuse
    if has_alembic_ini and has_wired_metadata:
        return True, "alembic.ini + env.py with wired target_metadata"
    if has_alembic_ini and revision_ok:
        detail = "alembic.ini + revision with real DDL upgrade()"
        if has_url_wiring:
            detail += " + env URL wiring"
        return True, detail
    if revision_refuse:
        return False, revision_refuse
    if has_theater_env:
        return False, (
            "alembic env sets target_metadata = None without a real DDL revision "
            "(theater stub — refuse Scan PR)"
        )
    if has_alembic_ini:
        return False, "alembic.ini without wired metadata or real DDL upgrade() revision"
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



def verify_sbom_ci(files: list[dict]) -> tuple[bool, str]:
    """True when staged files add CI SBOM generation (not a static BOM file).

    Product path: ``anchore/sbom-action`` / Syft in workflow, or Tekton
    Pipeline that wires an sbom step. Refuses bare ``sbom-task`` and static
    ``sbom.cdx.json`` as clear evidence.
    """
    from agentit.remediation.sbom_ci import staged_has_ci_sbom

    return staged_has_ci_sbom(files)


def verify_sbom_file(files: list[dict]) -> tuple[bool, str]:
    """Legacy: CycloneDX/SPDX artifact clear (demoted; primary path is ``sbom_ci``).

    Kept for fallback / older tests. Refuses empty ``{}`` theater and
    CycloneDX shells with ``components: []``.
    """
    import json

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
        if re.search(r"^\s*kind:\s*Task\s*$", body, re.I | re.M):
            return False, (
                f"{path}: Tekton Task does not clear app-repo SBOM finding "
                "(need CycloneDX/SPDX artifact — refuse sbom-task theater)"
            )
        if "bomformat" in low and "cyclonedx" in low:
            try:
                doc = json.loads(body)
            except json.JSONDecodeError:
                return False, f"{path}: CycloneDX marker but invalid JSON"
            comps = doc.get("components") if isinstance(doc, dict) else None
            if not isinstance(comps, list) or len(comps) == 0:
                return False, (
                    f"{path}: CycloneDX shell with empty components[] "
                    "(refuse — need inventory from Syft or lockfiles/manifests)"
                )
            return True, f"CycloneDX SBOM ({len(comps)} component(s)) in {path}"
        if "spdxversion" in low or '"spdxid"' in low or "spdx-license-id" in low:
            # SPDX: refuse trivial Document-only shells (no packages).
            if re.search(r'"packages"\s*:\s*\[\s*\]', body) or (
                '"packages"' in low and not re.search(r'"packages"\s*:\s*\[\s*\{', body)
            ):
                return False, (
                    f"{path}: SPDX shell with empty packages[] "
                    "(refuse trivial SBOM theater)"
                )
            return True, f"SPDX SBOM in {path}"
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


_SCANNER_CMD = re.compile(r"\b(trivy|grype|snyk)(?:\s|$)", re.IGNORECASE)
_STEP_IMAGE_LINE = re.compile(r"^\s+image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
_IMAGE_LATEST = re.compile(r":latest(?:@sha256:[0-9a-f]+)?$", re.IGNORECASE)


def verify_image_scan_task(files: list[dict]) -> tuple[bool, str]:
    """True when a Tekton Task runs trivy/grype/snyk with pinned step images.

    Mirrors ``cosign_sign_task``: refuse empty Task stubs. Also refuse
    ``:latest`` on any step image (same pin bar as Dockerfile FROM).
    """
    staged = _staged_map(files)
    tasks: list[tuple[str, str]] = []
    for path, content in staged.items():
        if re.search(r"^\s*kind:\s*(Cluster)?Task\s*$", content, re.I | re.M):
            tasks.append((path, content))
    if not tasks:
        return False, "no Tekton Task/ClusterTask in staged files"
    for path, content in tasks:
        if re.search(r"^\s*steps:\s*\[\s*\]\s*$", content, re.I | re.M):
            return False, f"{path}: empty Task steps[] — refuse theater"
        if not re.search(r"^\s+-\s+name:\s*\S+", content, re.M) and not re.search(
            r"steps:\s*\n\s+-", content,
        ):
            # No step entries under steps:
            if re.search(r"steps:\s*$", content, re.M) and not _SCANNER_CMD.search(content):
                return False, f"{path}: empty Task — refuse theater"
        for img in _STEP_IMAGE_LINE.findall(content):
            if _IMAGE_LATEST.search(img.strip()):
                return False, (
                    f"{path}: step image uses :latest ({img}) — "
                    "refuse (pin Trivy/UBI like Dockerfile FROM)"
                )
        if _SCANNER_CMD.search(content):
            return True, f"image-scan Task (trivy/grype/snyk) with pinned images in {path}"
    return False, (
        f"{tasks[0][0]}: Task without trivy/grype/snyk scan step "
        "(refuse empty or non-scanning Task theater)"
    )


def verify_grafana_dashboard(files: list[dict]) -> tuple[bool, str]:
    """True when a ConfigMap has grafana_dashboard label + non-empty panels."""
    import json

    staged = _staged_map(files)
    cms: list[tuple[str, str]] = []
    for path, content in staged.items():
        if re.search(r"^\s*kind:\s*ConfigMap\s*$", content, re.I | re.M):
            cms.append((path, content))
    if not cms:
        return False, "no ConfigMap in staged files"
    for path, content in cms:
        if not re.search(r"grafana_dashboard\s*:\s*[\"']?1[\"']?", content, re.I):
            continue
        # Extract dashboard JSON from data: block (literal | or inline).
        panels_ok = False
        # Prefer parsing JSON fragments that contain "panels".
        for m in re.finditer(r"\{[^{}]*\"panels\"\s*:\s*\[.*?\]\s*[,}]", content, re.S):
            try:
                doc = json.loads(m.group(0) if m.group(0).endswith("}") else m.group(0) + "}")
            except json.JSONDecodeError:
                continue
            panels = doc.get("panels") if isinstance(doc, dict) else None
            if isinstance(panels, list) and len(panels) > 0:
                panels_ok = True
                break
        if not panels_ok:
            # Looser: non-empty panels array in the YAML literal.
            if re.search(r'"panels"\s*:\s*\[\s*\{', content):
                panels_ok = True
            elif re.search(r'"panels"\s*:\s*\[\s*\]', content):
                return False, (
                    f"{path}: grafana_dashboard label but empty panels[] "
                    "(refuse empty dashboard shell)"
                )
            else:
                return False, (
                    f"{path}: grafana_dashboard label but no panels JSON "
                    "(refuse empty dashboard shell)"
                )
        if panels_ok:
            return True, f"Grafana dashboard ConfigMap with panels in {path}"
    return False, (
        f"{cms[0][0]}: ConfigMap without grafana_dashboard: \"1\" label "
        "+ non-empty panels (refuse empty dashboard shell)"
    )


_MATCH_LABELS_BLOCK = re.compile(
    r"selector:\s*\n(?:[ \t]+[^\n]+\n)*?[ \t]+matchLabels:\s*\n"
    r"((?:[ \t]+[^\n]+\n)+)",
    re.IGNORECASE,
)
_LABEL_LINE = re.compile(r"^[ \t]+([A-Za-z0-9./_-]+):\s*[\"']?([^\"'\n]+?)[\"']?\s*$")


def _parse_match_labels(content: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    m = _MATCH_LABELS_BLOCK.search(content or "")
    if not m:
        # Flat single-line fallback
        flat = re.search(
            r"matchLabels:\s*\{\s*([^}]*)\s*\}", content or "", re.I,
        )
        if flat:
            for part in flat.group(1).split(","):
                if ":" not in part:
                    continue
                k, v = part.split(":", 1)
                labels[k.strip().strip("\"'")] = v.strip().strip("\"'")
        return labels
    for ln in m.group(1).splitlines():
        lm = _LABEL_LINE.match(ln)
        if lm:
            labels[lm.group(1)] = lm.group(2).strip()
    return labels


def _selector_matches_label_set(
    selector: dict[str, str], label_set: dict[str, str],
) -> bool:
    if not selector:
        return False
    for k, v in selector.items():
        if str(label_set.get(k) or "") != v:
            return False
    return True


def verify_selector_target(
    files: list[dict],
    kinds: frozenset[str],
    *,
    live_label_sets: list[dict[str, str]] | None = None,
) -> tuple[bool, str]:
    """PDB/ServiceMonitor must declare matchLabels; optionally resolve live.

    When ``live_label_sets`` is provided (HPA pattern), refuse zero-match —
    selector must match at least one live Service/workload label set.
    """
    if not kinds:
        return False, "contract missing evidence_params (expected K8s kinds)"
    staged = _staged_map(files)
    found: list[tuple[str, str, dict[str, str]]] = []
    for path, content in staged.items():
        for kind in kinds:
            if not re.search(rf"^\s*kind:\s*{re.escape(kind)}\s*$", content, re.I | re.M):
                continue
            labels = _parse_match_labels(content)
            found.append((path, kind, labels))
    if not found:
        return False, f"none of kinds {sorted(kinds)} found in staged files"
    for path, kind, labels in found:
        if not labels:
            return False, (
                f"{path}: {kind} selector.matchLabels empty — "
                "refuse zero-match theater"
            )
    if live_label_sets is not None:
        if not live_label_sets:
            return False, (
                f"{found[0][1]} selector cannot match — no live Services/"
                "workload labels discovered (refuse zero-match)"
            )
        for path, kind, labels in found:
            if not any(
                _selector_matches_label_set(labels, live) for live in live_label_sets
            ):
                sample = ", ".join(
                    str(sorted(s.items())[:3]) for s in live_label_sets[:3]
                )
                return False, (
                    f"{path}: {kind} selector {labels} matches no live "
                    f"Services/workloads [{sample}] — refuse zero-match"
                )
        return True, (
            f"{found[0][1]} selector matches live label set "
            f"({found[0][2]})"
        )
    return True, f"{found[0][1]} selector.matchLabels {found[0][2]} (shape ok)"


def verify_argocd_application(
    files: list[dict],
    *,
    tree_paths: list[str] | None = None,
) -> tuple[bool, str]:
    """True when an Argo CD Application has repoURL + path or chart.

    Refuses empty Application shells and ``path: deploy/`` when the repo
    tree is known and that directory is missing.
    """
    staged = _staged_map(files)
    apps: list[tuple[str, str]] = []
    for path, content in staged.items():
        if re.search(r"^\s*kind:\s*(Application|ApplicationSet)\s*$", content, re.I | re.M):
            apps.append((path, content))
    if not apps:
        return False, "no Argo CD Application/ApplicationSet in staged files"
    for path, content in apps:
        if not re.search(r"^\s*kind:\s*Application\s*$", content, re.I | re.M):
            # ApplicationSet: require a template source shape
            if "repoURL:" in content and (
                re.search(r"^\s*path:\s*\S+", content, re.M)
                or re.search(r"^\s*chart:\s*\S+", content, re.M)
            ):
                return True, f"ApplicationSet with source in {path}"
            continue
        repo = re.search(r"^\s*repoURL:\s*[\"']?(\S+?)[\"']?\s*$", content, re.M)
        if not repo or not repo.group(1).strip() or repo.group(1).strip() in ("''", '""'):
            return False, f"{path}: Application missing spec.source.repoURL — refuse empty shell"
        repo_url = repo.group(1).strip().strip("\"'")
        if "example.com" in repo_url or repo_url in ("https://github.com/org/agentit.git",):
            # Template placeholder still ok if path/chart present — only refuse
            # when combined with missing tree path below.
            pass
        path_m = re.search(r"^\s*path:\s*[\"']?([^\s\"']+)[\"']?\s*$", content, re.M)
        chart_m = re.search(r"^\s*chart:\s*[\"']?([^\s\"']+)[\"']?\s*$", content, re.M)
        if not path_m and not chart_m:
            return False, (
                f"{path}: Application missing spec.source.path or chart — "
                "refuse empty shell"
            )
        src_path = (path_m.group(1).strip().strip("/'") if path_m else "")
        if tree_paths is not None and src_path:
            norm_tree = {p.replace("\\", "/").strip("/") for p in tree_paths}
            prefix = src_path.strip("/")
            # path exists if any tree entry equals or is under prefix
            exists = any(
                t == prefix or t.startswith(prefix + "/") for t in norm_tree
            )
            if not exists:
                return False, (
                    f"{path}: Application path {src_path!r} missing from repo "
                    "tree — refuse bogus deploy/ (or skip until chart exists)"
                )
        return True, f"Argo CD Application source ({repo_url} / {src_path or chart_m.group(1)}) in {path}"
    return False, (
        f"{apps[0][0]}: Application without repoURL + path/chart "
        "(refuse empty Application theater)"
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
    live_label_sets: list[dict[str, str]] | None = None,
    tree_paths: list[str] | None = None,
    finding_description: str = "",
) -> tuple[bool, str]:
    """Dispatch to the verifier for ``evidence_kind``."""
    params = evidence_params or frozenset()
    if evidence_kind == DETECT_ONLY:
        return False, "detect_only / no_auto_pr — Scan must not open a PR"
    if evidence_kind in (DOCKERFILE_PIN, BASE_IMAGE_PIN):
        return verify_dockerfile_pin(
            files, finding_description=finding_description,
        )
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
    if evidence_kind == IMAGE_SCAN_TASK:
        return verify_image_scan_task(files)
    if evidence_kind == GRAFANA_DASHBOARD:
        return verify_grafana_dashboard(files)
    if evidence_kind == SELECTOR_TARGET:
        return verify_selector_target(
            files, params, live_label_sets=live_label_sets,
        )
    if evidence_kind == ARGOCD_APPLICATION:
        return verify_argocd_application(files, tree_paths=tree_paths)
    if evidence_kind == SBOM_CI:
        return verify_sbom_ci(files)
    if evidence_kind == SBOM_FILE:
        return verify_sbom_file(files)
    return False, f"unknown evidence_kind {evidence_kind!r}"


def simulate_finding_clearance(
    files: list[dict],
    target_findings: list[tuple[str, str]],
    *,
    live_workloads: list[dict[str, str]] | None = None,
    live_label_sets: list[dict[str, str]] | None = None,
    tree_paths: list[str] | None = None,
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
    for cat, desc in target_findings:
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
        kind = contract.evidence_kind
        ok, reason = verify_evidence(
            kind,
            check_files,
            evidence_params=contract.evidence_params,
            live_workloads=live_workloads if kind == HPA_TARGET else None,
            live_label_sets=live_label_sets if kind == SELECTOR_TARGET else None,
            tree_paths=tree_paths if kind == ARGOCD_APPLICATION else None,
            finding_description=str(desc or ""),
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
    live_label_sets: list[dict[str, str]] | None = None,
    tree_paths: list[str] | None = None,
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
        live_label_sets=live_label_sets,
        tree_paths=tree_paths,
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
