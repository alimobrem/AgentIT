"""Deterministic source-repo patches that clear analyzer findings.

Skills with ``delivery: source`` call here instead of emitting K8s YAML.
Each generator returns ``GeneratedFile`` values with ``target_path`` set to
a real path in the app repo (Dockerfile, package.json, audit.py, …).
"""
from __future__ import annotations

import logging
import re
import textwrap

from typing import TYPE_CHECKING

from agentit.agents.base import GeneratedFile
from agentit.models import AssessmentReport, Finding

if TYPE_CHECKING:
    from agentit.skill_engine import Skill

logger = logging.getLogger(__name__)

_LATEST_TAG_RE = re.compile(r"(FROM\s+\S+):latest(\b)", re.IGNORECASE | re.MULTILINE)
_FROM_LINE_RE = re.compile(r"^\s*FROM\s+\S+", re.IGNORECASE | re.MULTILINE)
_USER_LINE_RE = re.compile(r"^\s*USER\s+", re.IGNORECASE | re.MULTILINE)
_HEALTHCHECK_LINE_RE = re.compile(r"^\s*HEALTHCHECK\s+", re.IGNORECASE | re.MULTILINE)
_UBI_FROM_RE = re.compile(
    r"^\s*FROM\s+\S*(?:ubi|redhat|registry\.access\.redhat)\S*",
    re.IGNORECASE | re.MULTILINE,
)

# Instructions that indicate a real app image (not our greenfield stub).
_SUBSTANTIVE_DF_TOKENS = ("RUN ", "COPY ", "ADD ", "ARG ", "ENV ", "WORKDIR ")

_UBI_RUNTIME: dict[str, str] = {
    "python": "registry.access.redhat.com/ubi9/python-312:1",
    "go": "registry.access.redhat.com/ubi9/ubi-minimal:1",
    "java": "registry.access.redhat.com/ubi9/openjdk-21:1",
    "node": "registry.access.redhat.com/ubi9/nodejs-20:1",
    "javascript": "registry.access.redhat.com/ubi9/nodejs-20:1",
    "typescript": "registry.access.redhat.com/ubi9/nodejs-20:1",
}


def _open_findings(report: AssessmentReport, category: str) -> list[Finding]:
    out: list[Finding] = []
    for score in report.scores:
        for finding in score.findings:
            if (finding.category or "").lower().replace("-", "_") == category:
                out.append(finding)
    return out


def _primary_language(report: AssessmentReport) -> str:
    if not report.stack.languages:
        return "python"
    return (report.stack.languages[0].name or "python").lower()


def pin_dockerfile_from_lines(content: str, *, floating_tag: str = "1") -> str:
    """Pin ``:latest`` on FROM lines only; leave the rest of the file untouched.

    Uses the floating major stream ``:1`` (UBI/Node/Python) by default —
    more reproducible than ``:latest`` without a live registry digest
    lookup. When ``floating_tag`` is a ``sha256:…`` digest, rewrite
    ``:latest`` FROM lines to ``image@sha256:…``.
    """
    tag = (floating_tag or "1").lstrip(":")
    if tag.startswith("sha256:"):
        return re.sub(
            r"(FROM\s+)(\S+):latest(\b)",
            rf"\1\2@{tag}\3",
            content,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    return _LATEST_TAG_RE.sub(rf"\1:{tag}\2", content)


def _pin_latest_in_dockerfile(content: str) -> str:
    """Replace ``:latest`` on FROM lines with a floating major tag ``:1``."""
    return pin_dockerfile_from_lines(content, floating_tag="1")


def harden_dockerfile_content(
    content: str,
    *,
    add_user: bool = False,
    add_healthcheck: bool = False,
    force_ubi: bool = False,
    language: str = "python",
    port: int = 8080,
) -> str:
    """Pin ``:latest`` and optionally add USER / HEALTHCHECK / UBI FROM.

    Additive harden for existing Dockerfiles — never a greenfield rewrite.
    """
    body = content if (content or "").endswith("\n") else (content or "") + "\n"
    if force_ubi and not _UBI_FROM_RE.search(body):
        from agentit.remediation.base_image import patch_base_image

        # patch_base_image uses :latest streams; re-pin to :1 afterward.
        patched = patch_base_image(body, language)
        if patched:
            body = patched
        else:
            ubi = _UBI_RUNTIME.get(language.lower(), "registry.access.redhat.com/ubi9/ubi-minimal:1")
            lines = body.splitlines(keepends=True)
            from_idxs = [
                i for i, ln in enumerate(lines)
                if re.match(r"^\s*FROM\s+", ln, re.IGNORECASE)
            ]
            if from_idxs:
                idx = from_idxs[-1]
                m = re.match(r"^(\s*FROM\s+)(\S+)(.*)", lines[idx], re.IGNORECASE)
                if m:
                    lines[idx] = f"{m.group(1)}{ubi}{m.group(3)}"
                    if not lines[idx].endswith("\n"):
                        lines[idx] += "\n"
                    body = "".join(lines)
    body = pin_dockerfile_from_lines(body)
    if add_user and not _USER_LINE_RE.search(body):
        body = body.rstrip("\n") + "\nUSER 1001\n"
    if add_healthcheck and not _HEALTHCHECK_LINE_RE.search(body):
        body = (
            body.rstrip("\n")
            + "\n"
            + f"HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\\n"
            + f"  CMD curl -f http://localhost:{port}/healthz || exit 1\n"
        )
    return body if body.endswith("\n") else body + "\n"


def _is_allowed_dockerfile_harden(existing: str, proposed: str) -> bool:
    """True when proposed is pin-only or additive USER/HEALTHCHECK/UBI harden."""
    exist = existing if existing.endswith("\n") else existing + "\n"
    prop = (proposed or "").strip()
    for add_user in (False, True):
        for add_hc in (False, True):
            for force_ubi in (False, True):
                for lang in ("python", "go", "java", "node", "javascript"):
                    hardened = harden_dockerfile_content(
                        exist,
                        add_user=add_user,
                        add_healthcheck=add_hc,
                        force_ubi=force_ubi,
                        language=lang,
                    ).strip()
                    if hardened == prop:
                        return True
    return False


def is_destructive_dockerfile_rewrite(
    existing: str, proposed: str,
) -> tuple[bool, str]:
    """True when ``proposed`` guts an existing Dockerfile into a short stub.

    Pin-only and additive USER/HEALTHCHECK/UBI hardens of ``existing`` are
    never destructive. Used by clear-evidence and delivery enrichment so
    Scan never repeats the #165 class of PR (136-line → 11-line stub).
    """
    if not (existing or "").strip():
        return False, "no existing file"
    exist = existing if existing.endswith("\n") else existing + "\n"
    prop = proposed if (proposed or "").endswith("\n") else (proposed or "") + "\n"
    pinned = pin_dockerfile_from_lines(exist)
    if prop.strip() == pinned.strip():
        return False, "pin-only of existing"
    if _is_allowed_dockerfile_harden(exist, prop):
        return False, "additive harden of existing"
    exist_body = [
        ln for ln in exist.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    prop_body = [
        ln for ln in prop.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if len(exist_body) >= 8 and len(prop_body) < max(6, len(exist_body) // 3):
        return True, (
            f"proposed Dockerfile guts existing "
            f"({len(exist_body)} → {len(prop_body)} non-comment lines)"
        )
    for token in _SUBSTANTIVE_DF_TOKENS:
        if exist.count(token) > prop.count(token):
            return True, f"proposed drops {token.strip()} instructions from existing Dockerfile"
    # Non-FROM body diverged beyond a pin — refuse wholesale rewrites.
    def _body_without_from(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines() if not _FROM_LINE_RE.match(ln)
        ).strip()

    if _body_without_from(exist) != _body_without_from(prop):
        return True, "proposed rewrites Dockerfile body (pin-only allowed)"
    return False, "ok"


def _harden_flags_for_findings(
    target_findings: list[tuple[str, str]] | None,
    target_path: str,
) -> tuple[bool, bool, bool]:
    """Derive (add_user, add_healthcheck, force_ubi) for one Dockerfile path."""
    add_user = add_hc = force_ubi = False
    target_cf = (target_path or "").casefold()
    base = target_cf.rsplit("/", 1)[-1]
    for cat, desc in target_findings or []:
        if (cat or "").lower().replace("-", "_") not in ("container", "dockerfile"):
            continue
        d = (desc or "").lower()
        # Path-bound when description names a file; else apply to this target.
        if " in " in d:
            named = d.rsplit(" in ", 1)[-1].strip()
            if named and named.casefold() not in (target_cf, base):
                continue
        if "healthcheck" in d:
            add_hc = True
        if "runs as root" in d or "no user directive" in d:
            add_user = True
        if "not ubi" in d or "universal base image" in d:
            force_ubi = True
    return add_user, add_hc, force_ubi


def apply_containerfile_pin_only(
    files: list[dict],
    *,
    read_file=None,
    target_findings: list[tuple[str, str]] | None = None,
    language: str = "python",
) -> list[dict]:
    """Rewrite containerfile outputs to pin/harden when the target exists.

    ``read_file(path) -> str | None`` fetches the default-branch content
    (GitHub REST). When the target exists, staged content becomes pin-only
    plus additive USER / HEALTHCHECK / UBI FROM for matching findings, and
    ``base_content`` is set for clear-evidence. Greenfield keeps the stub.
    """
    out: list[dict] = []
    for f in files:
        skill = (f.get("skill_name") or "").lower().replace("_", "-")
        target = str(f.get("target_path") or f.get("path") or "")
        is_df = (
            "dockerfile" in target.lower()
            or "containerfile" in target.lower()
            or target.lower().endswith("dockerfile")
            or target.lower().endswith("containerfile")
        )
        if skill not in ("containerfile", "eol-upgrade") or not is_df:
            out.append(f)
            continue
        existing = f.get("base_content")
        if existing is None and read_file is not None:
            try:
                existing = read_file(target)
            except Exception:
                logger.info("container pin-only: read_file failed for %s", target, exc_info=True)
                existing = None
        if not existing:
            # Greenfield — keep stub (no Dockerfile yet).
            out.append(f)
            continue
        add_user, add_hc, force_ubi = _harden_flags_for_findings(
            target_findings, target,
        )
        hardened = harden_dockerfile_content(
            existing,
            add_user=add_user,
            add_healthcheck=add_hc,
            force_ubi=force_ubi,
            language=language,
        )
        destructive, reason = is_destructive_dockerfile_rewrite(
            existing, f.get("content") or "",
        )
        new_f = dict(f)
        new_f["base_content"] = existing
        new_f["content"] = hardened
        desc = (f.get("description") or "").rstrip()
        harden_bits = []
        if add_user:
            harden_bits.append("USER")
        if add_hc:
            harden_bits.append("HEALTHCHECK")
        if force_ubi:
            harden_bits.append("UBI FROM")
        label = "pin+harden (" + ", ".join(harden_bits) + ")" if harden_bits else "pin-only FROM"
        if "pin-only" not in desc.lower() and "harden" not in desc.lower():
            new_f["description"] = (
                f"{desc} — {label} (no rewrite)" if desc else f"{label} (no rewrite)"
            )
        if destructive:
            logger.info(
                "container harden: replaced destructive stub for %s (%s)",
                target, reason,
            )
        out.append(new_f)
    return out


def _dockerfile_for_stack(lang: str, port: int = 8080) -> str:
    base = {
        "python": "registry.access.redhat.com/ubi9/python-312:1",
        "go": "registry.access.redhat.com/ubi9/go-toolset:1",
        "java": "registry.access.redhat.com/ubi9/openjdk-21:1",
        "node": "registry.access.redhat.com/ubi9/nodejs-20:1",
        "javascript": "registry.access.redhat.com/ubi9/nodejs-20:1",
        "typescript": "registry.access.redhat.com/ubi9/nodejs-20:1",
    }.get(lang, "registry.access.redhat.com/ubi9/ubi-minimal:1")
    return textwrap.dedent(f"""\
        FROM {base}

        WORKDIR /app
        COPY . .

        USER 1001

        EXPOSE {port}

        HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
          CMD curl -f http://localhost:{port}/healthz || exit 1
    """)


def _containerfile_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Emit a Containerfile/Dockerfile source patch.

    Greenfield (no Dockerfile finding): emit the UBI stub.
    Existing-file findings (``:latest``, missing USER/HEALTHCHECK/non-UBI):
    emit a pin/harden *placeholder* that delivery enrichment replaces with
    ``harden_dockerfile_content(existing)`` — never a full rewrite stub
    (#165 class). Clear-evidence refuses destructive rewrites when
    ``base_content`` is present; accepts USER/HEALTHCHECK/UBI on the
    finding path.
    """
    findings = _open_findings(report, "container") + _open_findings(report, "dockerfile")
    lang = _primary_language(report)
    paths: list[str] = []
    for f in findings:
        if f.file_path and (
            "dockerfile" in f.file_path.lower()
            or "containerfile" in f.file_path.lower()
        ):
            if f.file_path not in paths:
                paths.append(f.file_path)

    if not paths:
        # Greenfield or path-less finding: single Dockerfile stub.
        content = _pin_latest_in_dockerfile(_dockerfile_for_stack(lang))
        addressed = findings[0].description if findings else skill.property_description
        return [GeneratedFile(
            path="patch-Dockerfile",
            content=content if content.endswith("\n") else content + "\n",
            description=(
                f"Generated by skill {skill.name} — greenfield "
                "Containerfile/Dockerfile"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="Dockerfile",
        )]

    out: list[GeneratedFile] = []
    for target in paths:
        path_findings = [f for f in findings if f.file_path == target]
        addressed = (
            path_findings[0].description if path_findings
            else skill.property_description
        )
        content = (
            f"# agentit-pin-only: delivery will pin/harden FROM on existing "
            f"{target}\n"
            f"FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
        )
        out.append(GeneratedFile(
            path=f"patch-{target.replace('/', '-')}",
            content=content,
            description=(
                f"Generated by skill {skill.name} — pin-only/harden on "
                f"existing {target} (no rewrite)"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path=target,
        ))
    return out


def _snapshot_file_text(path: str) -> str:
    try:
        from agentit.analyzers.snapshot import get_active_snapshot

        snap = get_active_snapshot()
        if snap is not None and hasattr(snap, "files"):
            return str(
                snap.files.get(path)
                or snap.files.get(f"./{path}")
                or "",
            )
    except Exception:
        return ""
    return ""


def _workload_yaml_paths(report: AssessmentReport) -> list[str]:
    from agentit.remediation.workload_patches import is_workload_manifest

    paths = [p.replace("\\", "/") for p in _repo_paths_from_report(report)]
    out: list[str] = []
    for path in paths:
        if not path.endswith((".yml", ".yaml")):
            continue
        text = _snapshot_file_text(path)
        if text and is_workload_manifest(text):
            out.append(path)
    return out


def _workload_replicas_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Bump Deployment/Rollout replicas to >= 2 (clears ``replicas`` finding)."""
    from agentit.remediation.workload_patches import patch_replicas

    findings = _open_findings(report, "replicas")
    addressed = findings[0].description if findings else skill.property_description
    paths = _workload_yaml_paths(report)
    if not paths:
        # No workload in snapshot — emit a minimal Deployment so clear-evidence
        # can still see replicas>=2 (greenfield / chart-values repos).
        app = (report.repo_name or "app").lower().replace("_", "-").replace(".", "-")
        content = textwrap.dedent(f"""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: {app}
            spec:
              replicas: 2
              selector:
                matchLabels:
                  app.kubernetes.io/name: {app}
              template:
                metadata:
                  labels:
                    app.kubernetes.io/name: {app}
                spec:
                  containers:
                    - name: {app}
                      image: {app}:1
                      ports:
                        - containerPort: 8080
            """)
        return [GeneratedFile(
            path="patch-workload-replicas",
            content=content,
            description=(
                f"Generated by skill {skill.name} — Deployment replicas: 2 "
                "(clears replicas finding)"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="deploy/deployment.yaml",
        )]

    out: list[GeneratedFile] = []
    for path in paths:
        existing = _snapshot_file_text(path)
        patched = patch_replicas(existing, replicas=2)
        out.append(GeneratedFile(
            path=f"patch-replicas-{path.replace('/', '-')}",
            content=patched if patched.endswith("\n") else patched + "\n",
            description=(
                f"Generated by skill {skill.name} — set replicas>=2 on {path}"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path=path,
        ))
    return out


def _health_probes_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Inject liveness/readiness probes into Deployment/Rollout YAML."""
    from agentit.remediation.workload_patches import patch_health_probes

    findings = _open_findings(report, "health")
    addressed = findings[0].description if findings else skill.property_description
    paths = _workload_yaml_paths(report)
    if not paths:
        app = (report.repo_name or "app").lower().replace("_", "-").replace(".", "-")
        content = textwrap.dedent(f"""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: {app}
            spec:
              replicas: 1
              selector:
                matchLabels:
                  app.kubernetes.io/name: {app}
              template:
                metadata:
                  labels:
                    app.kubernetes.io/name: {app}
                spec:
                  containers:
                    - name: {app}
                      image: {app}:1
                      ports:
                        - containerPort: 8080
                      livenessProbe:
                        tcpSocket:
                          port: 8080
                        initialDelaySeconds: 15
                        periodSeconds: 20
                      readinessProbe:
                        tcpSocket:
                          port: 8080
                        initialDelaySeconds: 5
                        periodSeconds: 10
            """)
        return [GeneratedFile(
            path="patch-workload-probes",
            content=content,
            description=(
                f"Generated by skill {skill.name} — Deployment with "
                "livenessProbe+readinessProbe (clears health finding)"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="deploy/deployment.yaml",
        )]

    out: list[GeneratedFile] = []
    for path in paths:
        existing = _snapshot_file_text(path)
        patched = patch_health_probes(existing)
        out.append(GeneratedFile(
            path=f"patch-probes-{path.replace('/', '-')}",
            content=patched if patched.endswith("\n") else patched + "\n",
            description=(
                f"Generated by skill {skill.name} — inject probes on {path}"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path=path,
        ))
    return out


def _eol_upgrade_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    findings = _open_findings(report, "eol")
    files: list[GeneratedFile] = []
    for finding in findings:
        desc = (finding.description or "").lower()
        target = finding.file_path or ""
        if "node" in desc:
            # Never overwrite package.json wholesale (would destroy scripts/
            # deps). Pin runtime via .node-version — analyzer prefers this
            # over engines.node when present (see analyzers/eol.py).
            files.append(GeneratedFile(
                path="patch-node-version",
                content="22\n",
                description=(
                    f"Generated by skill {skill.name} — pin Node 22 "
                    f"past EOL ({finding.description})"
                ),
                finding_addressed=finding.description,
                skill_name=skill.name,
                target_path=".node-version",
            ))
        elif "python" in desc:
            target = target or ".python-version"
            # Extract major.minor from finding text when present.
            ver_match = re.search(r"python\s+(\d+\.\d+)", desc)
            current = ver_match.group(1) if ver_match else "3.9"
            major, minor = current.split(".")
            bumped = f"{major}.{int(minor) + 1}" if major == "3" else "3.12"
            if float(bumped) < 3.12:
                bumped = "3.12"
            files.append(GeneratedFile(
                path="patch-python-version",
                content=f"{bumped}\n",
                description=(
                    f"Generated by skill {skill.name} — bump Python "
                    f"past EOL ({finding.description})"
                ),
                finding_addressed=finding.description,
                skill_name=skill.name,
                target_path=target if target.endswith((".python-version", "runtime.txt")) else ".python-version",
            ))
        elif finding.file_path and (
            "dockerfile" in finding.file_path.lower()
            or "containerfile" in finding.file_path.lower()
        ):
            # Pin-only marker — never gut an existing Dockerfile (same bar as
            # containerfile / #165). Delivery enrichment applies the pin.
            content = (
                f"# agentit-pin-only: delivery will pin FROM on existing "
                f"{finding.file_path}\n"
                f"FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
            )
            files.append(GeneratedFile(
                path=f"patch-eol-{finding.file_path.replace('/', '-')}",
                content=content,
                description=(
                    f"Generated by skill {skill.name} — pin-only FROM on "
                    f"existing {finding.file_path} (EOL base image)"
                ),
                finding_addressed=finding.description,
                skill_name=skill.name,
                target_path=finding.file_path,
            ))
    return files


def _app_audit_logging_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Emit a language-matched audit helper (root path is a stub only).

    Delivery must relocate into the app package and wire middleware
    *before* clear-evidence (``audit_wired`` refuses root-only ``audit.py``).
    See ``remediation/audit_wire.py`` + auto_delivery pre-enrich.
    """
    lang = _primary_language(report)
    if lang in ("javascript", "typescript", "node"):
        target = "audit.ts"
        content = textwrap.dedent("""\
            /**
             * Application audit logging for privileged actions and data access.
             * Wire into auth / admin / data-mutation handlers.
             */
            export type AuditEvent = {
              action: string;
              actor: string;
              resource: string;
              outcome: "success" | "failure";
              metadata?: Record<string, unknown>;
            };

            export function auditLog(event: AuditEvent): void {
              const record = {
                ts: new Date().toISOString(),
                type: "audit",
                ...event,
              };
              // Structured stdout — collected by the platform log pipeline.
              console.info(JSON.stringify(record));
            }
        """)
    elif lang == "go":
        target = "audit.go"
        content = textwrap.dedent("""\
            package audit

            import (
            \t"encoding/json"
            \t"log"
            \t"time"
            )

            // Event is an application audit record for privileged actions / data access.
            type Event struct {
            \tTS       time.Time `json:"ts"`
            \tType     string    `json:"type"`
            \tAction   string    `json:"action"`
            \tActor    string    `json:"actor"`
            \tResource string    `json:"resource"`
            \tOutcome  string    `json:"outcome"`
            }

            // Log writes a structured audit event to stdout for the log pipeline.
            func Log(action, actor, resource, outcome string) {
            \te := Event{
            \t\tTS: time.Now().UTC(), Type: "audit",
            \t\tAction: action, Actor: actor, Resource: resource, Outcome: outcome,
            \t}
            \tb, _ := json.Marshal(e)
            \tlog.Println(string(b))
            }
        """)
    else:
        target = "audit.py"
        content = textwrap.dedent('''\
            """Application audit logging for privileged actions and data access."""

            from __future__ import annotations

            import json
            import logging
            from datetime import UTC, datetime
            from typing import Any

            _log = logging.getLogger("audit")


            def audit_log(
                action: str,
                *,
                actor: str,
                resource: str,
                outcome: str = "success",
                metadata: dict[str, Any] | None = None,
            ) -> None:
                """Emit a structured audit event (stdout → platform log pipeline)."""
                record = {
                    "ts": datetime.now(UTC).isoformat(),
                    "type": "audit",
                    "action": action,
                    "actor": actor,
                    "resource": resource,
                    "outcome": outcome,
                }
                if metadata:
                    record["metadata"] = metadata
                _log.info("%s", json.dumps(record, default=str))
        ''')

    findings = _open_findings(report, "audit")
    addressed = findings[0].description if findings else skill.property_description
    return [GeneratedFile(
        path=f"patch-{target}",
        content=content if content.endswith("\n") else content + "\n",
        description=f"Generated by skill {skill.name} — app audit logging module",
        finding_addressed=addressed,
        skill_name=skill.name,
        target_path=target,
    )]


def _port_for_language(lang: str) -> int:
    """Same convention already established in ``agents/codechange.py``'s
    ``_fix_dockerfile`` -- reused (not reinvented) so this skill's port
    default agrees with the rest of this codebase's own precedent."""
    return 3000 if lang in ("node", "javascript", "typescript") else 8080


_HELM_CHART_FILE_RE = re.compile(
    r"===FILE:\s*(.+?)\s*===\n(.*?)(?=\n===FILE:|\n===END===|\Z)", re.DOTALL,
)
_HELM_CHART_MAX_FILES = 8
_HELM_REQUIRED_FILE = "Chart.yaml"
_HELM_YAML_TEMPLATE_DIR = "templates/"


def _parse_llm_multi_file_response(raw: str) -> dict[str, str]:
    """Parse skill body's ``===FILE: <path>===`` delimited multi-file
    protocol. Returns ``{relative_path: content}``, capped at
    ``_HELM_CHART_MAX_FILES`` entries (defense against a runaway response)."""
    files: dict[str, str] = {}
    for match in _HELM_CHART_FILE_RE.finditer(raw or ""):
        path = match.group(1).strip().lstrip("/")
        content = match.group(2)
        if not path or not content.strip():
            continue
        files[path] = content if content.endswith("\n") else content + "\n"
        if len(files) >= _HELM_CHART_MAX_FILES:
            break
    return files


def _validate_helm_chart_files(files: dict[str, str]) -> list[str]:
    """Real validation gate for a candidate Helm chart -- every failure
    reason returned here is why this skill refuses the LLM's output and
    falls back to the deterministic template instead of shipping something
    "plausible-looking" but wrong (per the plan's "a wrong chart is worse
    than no chart" principle).
    """
    import yaml

    from agentit.agents.base import validate_manifest
    from agentit.skill_engine import _PLACEHOLDER_RE

    errors: list[str] = []
    if _HELM_REQUIRED_FILE not in files:
        errors.append(f"missing required {_HELM_REQUIRED_FILE}")
        return errors

    try:
        chart_doc = yaml.safe_load(files[_HELM_REQUIRED_FILE])
    except yaml.YAMLError as exc:
        return [f"{_HELM_REQUIRED_FILE}: YAML parse error: {exc}"]
    if not isinstance(chart_doc, dict) or not chart_doc.get("name") or not chart_doc.get("version"):
        errors.append(f"{_HELM_REQUIRED_FILE}: missing 'name' or 'version'")

    has_k8s_manifest = False
    for path, content in files.items():
        if not path.startswith(_HELM_YAML_TEMPLATE_DIR) or not path.endswith((".yaml", ".yml")):
            continue
        if "apiVersion:" in content and "kind:" in content:
            has_k8s_manifest = True
        manifest_errors = validate_manifest(content)
        if manifest_errors:
            errors.append(f"{path}: {'; '.join(manifest_errors)}")
        unresolved = sorted(set(_PLACEHOLDER_RE.findall(content)))
        if unresolved:
            errors.append(f"{path}: unresolved placeholder(s): {', '.join(unresolved)}")

    if not has_k8s_manifest:
        errors.append(
            "no templates/*.yaml file has literal 'apiVersion:'/'kind:' text "
            "-- would not clear the 'manifests' finding"
        )
    return errors


_HELM_CHART_SYSTEM_PROMPT = (
    "You are a platform engineer scaffolding a real, minimal Helm chart for "
    "an application that currently has none. Output ONLY the delimited "
    "multi-file format described in the instructions -- no commentary, no "
    "markdown fences around the whole response. Never invent a hostname, "
    "Ingress/Route, or specific environment variable names/values -- omit "
    "them rather than guess. Never use Helm control-flow directives "
    "({{- if }}, {{- range }}, {{- with }}) in templates/*.yaml -- only "
    "inline value substitutions ({{ .Values.x }}), so every file stays "
    "parseable as plain YAML. ALWAYS quote a {{ .Values.x }}/{{ .Chart.x }} "
    "substitution when it starts a YAML scalar value (e.g. "
    "replicas: \"{{ .Values.replicaCount }}\", never "
    "replicas: {{ .Values.replicaCount }}) -- unquoted, a leading {{ "
    "parses as YAML flow-mapping syntax and fails to parse."
)


def _helm_chart_llm_user_prompt(
    skill: "Skill", report: AssessmentReport, app_name: str, image_ref: str,
) -> str:
    stack = ", ".join(l.name for l in report.stack.languages) if report.stack.languages else "unknown"
    frameworks = ", ".join(f.name for f in report.stack.frameworks) if report.stack.frameworks else "none detected"
    return (
        f"Application: {app_name}\n"
        f"Stack: {stack}\n"
        f"Frameworks: {frameworks}\n"
        f"Architecture: {report.architecture.architecture_style}, "
        f"has_api={report.architecture.has_api}, api_style={report.architecture.api_style}\n"
        f"Criticality: {report.criticality}\n"
        f"Real internal registry image reference to use in values.yaml: {image_ref}\n\n"
        f"Skill instructions:\n{skill.body}\n\n"
        "Generate Chart.yaml, values.yaml, templates/deployment.yaml, and "
        "templates/service.yaml for this application, using the exact "
        "===FILE: <path>=== / ===END=== delimited format from the "
        "instructions."
    )


def _helm_chart_llm_attempt(
    skill: "Skill", report: AssessmentReport, app_name: str, image_ref: str, llm_client: object,
) -> dict[str, str] | None:
    """Two-attempt LLM generation with validation-error feedback, mirroring
    ``SkillEngine._generate_with_llm``'s own retry shape. Returns the parsed
    ``{path: content}`` map on success, or ``None`` to fall back to the
    deterministic template."""
    from agentit.llm import _SKILL_GENERATION_MAX_TOKENS

    user = _helm_chart_llm_user_prompt(skill, report, app_name, image_ref)
    for attempt in range(2):
        raw = llm_client._chat(_HELM_CHART_SYSTEM_PROMPT, user, max_tokens=_SKILL_GENERATION_MAX_TOKENS)
        if raw is None:
            return None
        parsed = _parse_llm_multi_file_response(raw)
        errors = _validate_helm_chart_files(parsed)
        if not errors:
            return parsed
        logger.info(
            "helm-chart LLM generation for %s rejected (attempt %d): %s",
            app_name, attempt + 1, errors,
        )
        user += f"\n\nYour previous output had errors: {errors}. Fix them and resend the full format."
    return None


def _helm_chart_template_fallback(
    skill: "Skill", report: AssessmentReport, app_name: str, image_ref: str,
) -> dict[str, str]:
    """Deterministic, literal-values-only chart -- no Helm ``.Values``/
    ``.Release`` indirection at all, so there is zero risk of an
    unresolved-placeholder or mismatched-values-vs-template bug. Every
    value baked in here is one this skill already has real data for
    (``app_name``, the real ``image_ref``, and the same language-based port
    convention ``agents/codechange.py::_fix_dockerfile`` already uses) --
    nothing fabricated, matching the "no mock data" rule.
    """
    lang = _primary_language(report)
    port = _port_for_language(lang)
    repo_ref, _, tag = image_ref.rpartition(":")
    tag = tag or "latest"

    chart_yaml = textwrap.dedent(f"""\
        apiVersion: v2
        name: {app_name}
        description: Helm chart for {app_name}, generated by AgentIT to satisfy IaC/manifest baselines
        type: application
        version: 0.1.0
        appVersion: "1.0.0"
    """)
    values_yaml = textwrap.dedent(f"""\
        # Real values used to render templates/*.yaml. This chart intentionally
        # uses literal values rather than Helm's `.Values`/`.Release` indirection
        # -- see skills/infrastructure/helm-chart.md for why (deterministic
        # fallback, no LLM available for app-specific tailoring).
        replicaCount: 1
        image:
          repository: {repo_ref}
          tag: "{tag}"
        service:
          port: {port}
    """)
    deployment_yaml = textwrap.dedent(f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {app_name}
          labels:
            app.kubernetes.io/name: {app_name}
        spec:
          replicas: 1
          selector:
            matchLabels:
              app.kubernetes.io/name: {app_name}
          template:
            metadata:
              labels:
                app.kubernetes.io/name: {app_name}
            spec:
              containers:
                - name: {app_name}
                  image: "{image_ref}"
                  ports:
                    - name: http
                      containerPort: {port}
                  livenessProbe:
                    tcpSocket:
                      port: http
                    initialDelaySeconds: 15
                    periodSeconds: 20
                    failureThreshold: 5
                  readinessProbe:
                    tcpSocket:
                      port: http
                    initialDelaySeconds: 10
                    periodSeconds: 10
                    failureThreshold: 3
    """)
    service_yaml = textwrap.dedent(f"""\
        apiVersion: v1
        kind: Service
        metadata:
          name: {app_name}
          labels:
            app.kubernetes.io/name: {app_name}
        spec:
          type: ClusterIP
          selector:
            app.kubernetes.io/name: {app_name}
          ports:
            - port: {port}
              targetPort: http
              protocol: TCP
              name: http
    """)
    return {
        "Chart.yaml": chart_yaml,
        "values.yaml": values_yaml,
        "templates/deployment.yaml": deployment_yaml,
        "templates/service.yaml": service_yaml,
    }


def _helm_chart_patch(
    skill: "Skill", report: AssessmentReport, app_name: str, llm_client: object | None = None,
) -> list[GeneratedFile]:
    """Real Helm chart (Chart.yaml + values.yaml + Deployment + Service)
    clearing infrastructure.py's ``iac`` and ``manifests`` findings in one
    PR -- see skills/infrastructure/helm-chart.md for the full design
    rationale (why one skill, why LLM-mode, why no Ingress/env vars).
    """
    from agentit.image_builder import get_image_ref

    image_ref = get_image_ref(app_name)
    files_by_path: dict[str, str] | None = None

    if llm_client is not None and hasattr(llm_client, "_chat"):
        files_by_path = _helm_chart_llm_attempt(skill, report, app_name, image_ref, llm_client)
        source_note = "LLM-tailored"

    if not files_by_path:
        files_by_path = _helm_chart_template_fallback(skill, report, app_name, image_ref)
        source_note = "deterministic template (no LLM, or LLM output failed validation)"

    findings = _open_findings(report, "iac") + _open_findings(report, "manifests")
    addressed = "; ".join(f.description for f in findings) if findings else skill.property_description

    prefix = "helm"
    out: list[GeneratedFile] = []
    for rel_path, content in files_by_path.items():
        target = f"{prefix}/{rel_path}"
        out.append(GeneratedFile(
            path=f"patch-{target.replace('/', '-')}",
            content=content,
            description=f"Generated by skill {skill.name} — {source_note} Helm chart file",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path=target,
        ))
    return out


def _db_migration_tooling_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Real migration scaffolding — never config-only Alembic theater.

    Greenfield apps get Alembic (Python) or versioned SQL (Go/Node) with a
    non-empty first revision and URL read from the environment (no invented
    credentials). Clear-evidence refuses config-only stubs (#157).
    """
    lang = _primary_language(report)
    files: list[GeneratedFile] = []
    findings = _open_findings(report, "migration")
    addressed = findings[0].description if findings else skill.property_description

    if lang in ("javascript", "typescript", "node"):
        files.append(GeneratedFile(
            path="patch-migrations-0001",
            content=textwrap.dedent("""\
                -- 0001_init.up.sql — baseline schema revision (node-pg-migrate /
                -- prisma migrate / knex). Real DDL required (clear-evidence
                -- refuses SELECT 1 / empty stubs).
                CREATE TABLE IF NOT EXISTS schema_migrations_baseline (
                  id TEXT PRIMARY KEY,
                  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """),
            description=f"Generated by skill {skill.name} — first SQL migration",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="migrations/0001_init.up.sql",
        ))
        files.append(GeneratedFile(
            path="patch-migrations-0001-down",
            content=textwrap.dedent("""\
                -- 0001_init.down.sql — reverse of 0001_init.up.sql
                DROP TABLE IF EXISTS schema_migrations_baseline;
            """),
            description=f"Generated by skill {skill.name} — first SQL down migration",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="migrations/0001_init.down.sql",
        ))
    elif lang == "go":
        files.append(GeneratedFile(
            path="patch-migrate-0001-up",
            content=textwrap.dedent("""\
                -- +migrate Up
                -- Baseline revision for golang-migrate (real DDL; not SELECT 1).
                CREATE TABLE IF NOT EXISTS schema_migrations_baseline (
                  id TEXT PRIMARY KEY,
                  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """),
            description=f"Generated by skill {skill.name} — golang-migrate up",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="migrations/0001_init.up.sql",
        ))
        files.append(GeneratedFile(
            path="patch-migrate-0001-down",
            content=textwrap.dedent("""\
                -- +migrate Down
                DROP TABLE IF EXISTS schema_migrations_baseline;
            """),
            description=f"Generated by skill {skill.name} — golang-migrate down",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="migrations/0001_init.down.sql",
        ))
    else:
        files.append(GeneratedFile(
            path="patch-alembic-ini",
            content=textwrap.dedent("""\
                [alembic]
                script_location = alembic
                prepend_sys_path = .
                version_path_separator = os
                # sqlalchemy.url is set at runtime from DATABASE_URL /
                # SQLALCHEMY_URL / AGENTIT_DB_DSN in alembic/env.py — do not
                # commit credentials here.

                [loggers]
                keys = root,sqlalchemy,alembic

                [handlers]
                keys = console

                [formatters]
                keys = generic

                [logger_root]
                level = WARN
                handlers = console

                [logger_sqlalchemy]
                level = WARN
                handlers =
                qualname = sqlalchemy.engine

                [logger_alembic]
                level = INFO
                handlers =
                qualname = alembic

                [handler_console]
                class = StreamHandler
                args = (sys.stderr,)
                level = NOTSET
                formatter = generic

                [formatter_generic]
                format = %(levelname)-5.5s [%(name)s] %(message)s
            """),
            description=f"Generated by skill {skill.name} — Alembic config",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="alembic.ini",
        ))
        files.append(GeneratedFile(
            path="patch-alembic-env",
            content=textwrap.dedent('''\
                """Alembic environment — URL from env; revisions under versions/."""

                from __future__ import annotations

                import os
                from logging.config import fileConfig

                from alembic import context
                from sqlalchemy import engine_from_config, pool

                config = context.config
                if config.config_file_name is not None:
                    fileConfig(config.config_file_name)

                # Prefer the deploy-time DSN; never commit secrets into alembic.ini.
                db_url = (
                    os.environ.get("DATABASE_URL")
                    or os.environ.get("SQLALCHEMY_URL")
                    or os.environ.get("AGENTIT_DB_DSN")
                )
                if db_url:
                    config.set_main_option("sqlalchemy.url", db_url)

                # Manual revisions under alembic/versions/ do not require
                # MetaData. For autogenerate, import your models' MetaData:
                #   from myapp.models import Base
                #   target_metadata = Base.metadata
                target_metadata = None  # intentional for manual revisions


                def run_migrations_offline() -> None:
                    url = config.get_main_option("sqlalchemy.url")
                    context.configure(
                        url=url,
                        target_metadata=target_metadata,
                        literal_binds=True,
                        dialect_opts={"paramstyle": "named"},
                    )
                    with context.begin_transaction():
                        context.run_migrations()


                def run_migrations_online() -> None:
                    connectable = engine_from_config(
                        config.get_section(config.config_ini_section, {}),
                        prefix="sqlalchemy.",
                        poolclass=pool.NullPool,
                    )
                    with connectable.connect() as connection:
                        context.configure(connection=connection, target_metadata=target_metadata)
                        with context.begin_transaction():
                            context.run_migrations()


                if context.is_offline_mode():
                    run_migrations_offline()
                else:
                    run_migrations_online()
            '''),
            description=f"Generated by skill {skill.name} — Alembic env.py",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="alembic/env.py",
        ))
        files.append(GeneratedFile(
            path="patch-alembic-revision",
            content=textwrap.dedent('''\
                """Baseline schema revision — real DDL (clear-evidence refuses pass/SELECT 1)."""

                from __future__ import annotations

                from alembic import op

                revision = "0001_baseline"
                down_revision = None
                branch_labels = None
                depends_on = None


                def upgrade() -> None:
                    op.execute(
                        "CREATE TABLE IF NOT EXISTS schema_migrations_baseline ("
                        "id TEXT PRIMARY KEY, "
                        "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
                    )


                def downgrade() -> None:
                    op.execute("DROP TABLE IF EXISTS schema_migrations_baseline")
            '''),
            description=f"Generated by skill {skill.name} — first Alembic revision",
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path="alembic/versions/0001_baseline.py",
        ))
    return files


def _repo_paths_from_report(report: AssessmentReport) -> list[str]:
    """Best-effort path list from an active snapshot (assessment-time)."""
    try:
        from agentit.analyzers.snapshot import get_active_snapshot

        snap = get_active_snapshot()
        if snap is not None:
            return list(snap.file_paths())
    except Exception:
        pass
    del report
    return []


def _sbom_ci_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Wire CI SBOM generation so compliance ``sbom`` clears on re-Assess.

    Prefer GitHub Actions when workflows exist; else Tekton Pipeline wire;
    else create ``.github/workflows/sbom.yml``.
    """
    from agentit.remediation.sbom_ci import (
        content_has_gha_or_ci_sbom,
        default_gha_sbom_workflow,
        gha_sbom_step_snippet,
    )

    findings = _open_findings(report, "sbom")
    addressed = findings[0].description if findings else skill.property_description
    paths = [p.replace("\\", "/") for p in _repo_paths_from_report(report)]
    workflow_paths = sorted(
        p for p in paths
        if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
    )
    pipeline_paths = sorted(
        p for p in paths
        if p.endswith((".yml", ".yaml")) and "pipeline" in p.lower()
    )

    # Prefer extending an existing GHA workflow when present.
    if workflow_paths:
        target = workflow_paths[0]
        existing = ""
        try:
            from agentit.analyzers.snapshot import get_active_snapshot

            snap = get_active_snapshot()
            if snap is not None and hasattr(snap, "files"):
                existing = str(snap.files.get(target) or snap.files.get(f"./{target}") or "")
        except Exception:
            existing = ""
        if existing and content_has_gha_or_ci_sbom(existing):
            # Already present — still emit a no-op-safe dedicated workflow so
            # clear-evidence sees staged CI content when snapshot was stale.
            content = default_gha_sbom_workflow()
            target = ".github/workflows/sbom.yml"
            desc = (
                f"Generated by skill {skill.name} — ensure GHA SBOM generation "
                "(anchore/sbom-action; clears compliance sbom)"
            )
        elif existing and "steps:" in existing:
            step = gha_sbom_step_snippet()
            if not existing.endswith("\n"):
                existing += "\n"
            content = existing + step
            desc = (
                f"Generated by skill {skill.name} — add anchore/sbom-action "
                f"step to {target} (clears compliance sbom)"
            )
        else:
            content = default_gha_sbom_workflow()
            target = ".github/workflows/sbom.yml"
            desc = (
                f"Generated by skill {skill.name} — GitHub Action SBOM "
                "workflow (anchore/sbom-action; clears compliance sbom)"
            )
        return [GeneratedFile(
            path="patch-sbom-ci-gha",
            content=content if content.endswith("\n") else content + "\n",
            description=desc,
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path=target,
        )]

    # Tekton-only repos: emit a Pipeline fragment that wires sbom-generate.
    if pipeline_paths and not workflow_paths:
        app = (report.repo_name or "app").lower().replace("_", "-").replace(".", "-")
        content = textwrap.dedent(f"""\
            apiVersion: tekton.dev/v1
            kind: Pipeline
            metadata:
              name: {app}-pipeline
            spec:
              tasks:
                - name: sbom-generate
                  taskRef:
                    name: {app}-sbom
                  params:
                    - name: IMAGE
                      value: $(params.IMAGE)
            """)
        return [GeneratedFile(
            path="patch-sbom-ci-tekton",
            content=content,
            description=(
                f"Generated by skill {skill.name} — Tekton Pipeline wires "
                "sbom-generate (clears compliance sbom; not bare Task alone)"
            ),
            finding_addressed=addressed,
            skill_name=skill.name,
            target_path=f"{app}-sbom-pipeline.yaml",
        )]

    # Default: new GHA workflow (easy to add even when no CI exists yet).
    content = default_gha_sbom_workflow()
    return [GeneratedFile(
        path="patch-sbom-ci-gha",
        content=content,
        description=(
            f"Generated by skill {skill.name} — GitHub Action SBOM workflow "
            "(anchore/sbom-action; clears compliance sbom)"
        ),
        finding_addressed=addressed,
        skill_name=skill.name,
        target_path=".github/workflows/sbom.yml",
    )]


def _sbom_artifact_patch(skill: "Skill", report: AssessmentReport) -> list[GeneratedFile]:
    """Demoted fallback: CycloneDX file (does not clear ``sbom_ci`` evidence).

    Prefer inventory from an active ``RepoSnapshot`` (assessment-time) so the
    staged file already has real ``components``. Delivery enrichment
    (``enrich_sbom_artifact_files``) fills from GitHub manifests / Syft when
    the generator only had an empty shell — clear-evidence refuses ``[]``.
    """
    import json

    from agentit.remediation.sbom_build import (
        build_cyclonedx_document,
        collect_manifests,
        components_from_manifests,
    )

    app = (report.repo_name or "app").lower().replace("_", "-").replace(".", "-")
    findings = _open_findings(report, "sbom")
    addressed = findings[0].description if findings else skill.property_description

    snapshot_files = None
    try:
        from agentit.analyzers.snapshot import get_active_snapshot

        snap = get_active_snapshot()
        if snap is not None:
            snapshot_files = snap.files
    except Exception:
        snapshot_files = None

    manifests = collect_manifests(snapshot_files=snapshot_files)
    components = components_from_manifests(manifests)
    doc = build_cyclonedx_document(app, components)
    n = len(components)
    desc = (
        f"Generated by skill {skill.name} — CycloneDX SBOM artifact "
        f"({n} component(s); clears compliance sbom finding)"
    )
    content = json.dumps(doc, indent=2) + "\n"
    return [GeneratedFile(
        path="patch-sbom-cdx-json",
        content=content,
        description=desc,
        finding_addressed=addressed,
        skill_name=skill.name,
        target_path="sbom.cdx.json",
    )]


def enrich_sbom_from_repo(
    files: list[dict],
    *,
    read_file=None,
    tree_paths: list[str] | None = None,
    repo_path=None,
    app_name: str | None = None,
) -> list[dict]:
    """Delivery-time SBOM populate (Syft or manifest inventory)."""
    from agentit.remediation.sbom_build import enrich_sbom_artifact_files

    return enrich_sbom_artifact_files(
        files,
        read_file=read_file,
        tree_paths=tree_paths,
        repo_path=repo_path,
        app_name=app_name,
    )


def generate_source_patch_for_skill(
    skill: "Skill",
    report: AssessmentReport,
    app_name: str,
    llm_client: object | None = None,
) -> list[GeneratedFile]:
    """Dispatch to the skill-specific source patch generator.

    ``llm_client`` is only actually used by ``helm-chart`` (the one
    ``delivery: source`` skill that needs LLM tailoring -- see
    skills/infrastructure/helm-chart.md); every other generator here is
    deterministic and ignores it, unchanged from before.
    """
    if skill.name == "helm-chart":
        return _helm_chart_patch(skill, report, app_name, llm_client)

    generators = {
        "containerfile": _containerfile_patch,
        "eol-upgrade": _eol_upgrade_patch,
        "app-audit-logging": _app_audit_logging_patch,
        "db-migration-tooling": _db_migration_tooling_patch,
        "sbom-ci": _sbom_ci_patch,
        "sbom-artifact": _sbom_artifact_patch,
        "workload-replicas": _workload_replicas_patch,
        "workload-health-probes": _health_probes_patch,
    }
    gen = generators.get(skill.name)
    if gen is None:
        from agentit.skill_engine import (
            _extract_template,
            _render_template,
            _template_variables,
        )

        # Generic: render a template fence if present, using first output
        # label as the target filename.
        template = _extract_template(skill.body)
        if not template:
            logger.info("Source skill %s has no template/generator — skipping", skill.name)
            return []
        target = skill.outputs[0] if skill.outputs else skill.name
        try:
            content = _render_template(
                template,
                _template_variables(
                    report.repo_name.lower().replace("_", "-").replace(".", "-"),
                    report,
                ),
            )
        except Exception as exc:
            logger.info("Source skill %s template failed: %s", skill.name, exc)
            return []
        return [GeneratedFile(
            path=f"patch-{target.replace('/', '-')}",
            content=content if content.endswith("\n") else content + "\n",
            description=f"Generated by skill {skill.name}",
            finding_addressed=skill.property_description,
            skill_name=skill.name,
            target_path=target,
        )]
    return gen(skill, report)
