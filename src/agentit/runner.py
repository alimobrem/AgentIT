from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from agentit.analyzers.base import is_ignored, calculate_score
from agentit.analyzers.cicd import CICDAnalyzer
from agentit.analyzers.compliance import ComplianceAnalyzer
from agentit.analyzers.data_governance import DataGovernanceAnalyzer
from agentit.analyzers.ha_dr import HADRAnalyzer
from agentit.analyzers.infrastructure import InfrastructureAnalyzer
from agentit.analyzers.observability import ObservabilityAnalyzer
from agentit.analyzers.security import SecurityAnalyzer
from agentit.analyzers.snapshot import RepoSnapshot, use_snapshot
from agentit.analyzers.stack_detector import StackDetector
from agentit.check_engine import load_checks, run_checks_by_dimension_with_status
from agentit.models import (
    ArchitectureInfo, AssessmentReport, DimensionScore, RemediationItem, Severity,
)

log = logging.getLogger(__name__)

# Stable analyzer order for deterministic DimensionScore list after concurrent run.
_ANALYZER_ORDER = (
    "security",
    "observability",
    "cicd",
    "infrastructure",
    "compliance",
    "data_governance",
    "ha_dr",
)

AGENT_MAP = {
    "security": "Security Hardening Agent",
    "observability": "Observability Bootstrap Agent",
    "cicd": "CI/CD & GitOps Agent",
    "infrastructure": "Infrastructure Codification Agent",
    "compliance": "Compliance & Policy Agent",
    "data_governance": "Data Governance Agent",
    "ha_dr": "HA/DR Agent",
}

EFFORT_MAP = {
    Severity.critical: "2 agent-hours",
    Severity.high: "1 agent-hour",
    Severity.medium: "30 agent-minutes",
    Severity.low: "15 agent-minutes",
    Severity.info: "5 agent-minutes",
}


def _default_checks_dir() -> Path:
    """Return the ``checks/`` directory at the project root."""
    return Path(__file__).resolve().parent.parent.parent / "checks"


def _default_skills_dir() -> Path:
    """Return the ``skills/`` directory at the project root -- the same
    default-resolution convention as ``_default_checks_dir()`` above, used
    here only to find ``mode: detect`` skills (see
    ``skill_engine.detect_check_definitions()``). Template/llm-mode skill
    matching for remediation is a separate, unrelated call path
    (``SkillEngine``, invoked from the onboarding flow, not from here)."""
    return Path(__file__).resolve().parent.parent.parent / "skills"


def run_assessment(
    repo_path: Path,
    repo_url: str,
    criticality: str = "medium",
    llm_client: object | None = None,
    infra_repo_url: str | None = None,
    checks_dir: Path | None = None,
    skills_dir: Path | None = None,
    suppressions: set[str] | None = None,
    check_results_out: list[dict] | None = None,
    secret_decisions_out: list[dict] | None = None,
) -> AssessmentReport:
    """Run a full assessment.

    ``check_results_out``, if provided, is populated (in place) with one
    ``{"check_name", "dimension", "passed"}`` row per data-driven check that
    ran — the caller (typically the portal, once it has an `assessment_id`)
    can then persist this via `AssessmentStore.save_check_results`. This
    includes both legacy ``checks/*.yaml`` files (from ``checks_dir``) and
    ``mode: detect`` Markdown skills (from ``skills_dir``) — see
    docs/extension-model-unification-plan-2026-07-18.md, Phase 1: both
    formats are converted to the same ``check_engine.CheckDefinition`` and
    run through the exact same engine, so a caller reading
    ``check_results_out`` cannot tell (and does not need to care) which
    format produced any given row.

    ``secret_decisions_out``, if provided, is populated (in place) with one
    row per real `classify_secret` LLM call the security analyzer made (see
    `analyzers.security.SecurityAnalyzer`) — the caller can persist these via
    `llm_decisions.build_secret_classify_events()` + `store.log_event()`, the
    same "populate then persist once an assessment_id exists" pattern as
    `check_results_out`.
    """
    # Analyzers call Path.relative_to(repo_path); snapshot paths are absolute
    # under the resolved root — keep one canonical path for the whole run.
    repo_path = repo_path.resolve()
    snapshot = RepoSnapshot.build(repo_path)
    if snapshot.skipped_oversized:
        log.debug(
            "RepoSnapshot skipped %d oversized file(s) under %s",
            snapshot.skipped_oversized, repo_path,
        )

    with use_snapshot(snapshot):
        detector = StackDetector()
        stack = detector.detect(repo_path)

        architecture = _detect_architecture(repo_path, snapshot)

        analyzers = [
            SecurityAnalyzer(llm_client=llm_client, secret_decisions_out=secret_decisions_out),
            ObservabilityAnalyzer(),
            CICDAnalyzer(),
            InfrastructureAnalyzer(llm_client=llm_client),
            ComplianceAnalyzer(),
            DataGovernanceAnalyzer(),
            HADRAnalyzer(),
        ]

        scores = _run_analyzers_concurrent(analyzers, repo_path)

        # Fleet remediations (ResourceQuota/HPA) land in gitops → Argo → live
        # namespace. Source-only analyzers never see them; clear quota/scaling
        # when the live namespace already has the resource (finding-clear proof).
        repo_name_early = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        from agentit.analyzers.live_evidence import apply_live_cluster_finding_clear
        scores = apply_live_cluster_finding_clear(scores, repo_name_early)

        # Run data-driven checks -- both legacy checks/*.yaml files and
        # mode: detect Markdown skills (docs/extension-model-unification-plan-2026-07-18.md,
        # Phase 1) -- and merge findings into dimension scores.
        resolved_checks_dir = checks_dir if checks_dir is not None else _default_checks_dir()
        check_defs = load_checks(resolved_checks_dir)

        resolved_skills_dir = skills_dir if skills_dir is not None else _default_skills_dir()
        from agentit.skill_engine import detect_check_definitions, load_all_skills
        check_defs = check_defs + detect_check_definitions(load_all_skills(resolved_skills_dir))

        check_statuses: list[dict] = []
        if check_defs:
            scores, check_statuses = _merge_check_findings(scores, check_defs, repo_path)
            if check_results_out is not None:
                check_results_out.extend(check_statuses)

        if suppressions:
            scores = _apply_suppressions(scores, suppressions)

        # Score model v2: pass-ratio per dimension + criticality weights.
        from agentit.scoring import SCORE_VERSION_V2, apply_score_model_v2
        scores = apply_score_model_v2(scores, check_statuses, criticality)

        remediation_plan = generate_remediation_plan(scores)

        repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

        summary = _generate_summary(scores, repo_name)
        if llm_client is not None:
            llm_summary = llm_client.summarize_architecture(
                stack.model_dump(),
                snapshot.file_paths(),
            )
            if llm_summary is not None:
                summary = llm_summary

    return AssessmentReport(
        repo_url=repo_url,
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=stack,
        architecture=architecture,
        scores=scores,
        criticality=criticality,
        summary=summary,
        remediation_plan=remediation_plan,
        infra_repo_url=infra_repo_url or None,
        score_version=SCORE_VERSION_V2,
    )


def _run_analyzers_concurrent(analyzers: list, repo_path: Path) -> list[DimensionScore]:
    """Run analyzers in a thread pool; preserve stable dimension order."""
    if len(analyzers) <= 1:
        return [a.analyze(repo_path) for a in analyzers]

    by_dim: dict[str, DimensionScore] = {}
    max_workers = min(8, len(analyzers))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Fresh copy_context() per submit so workers inherit the active
        # RepoSnapshot without sharing one entered Context object.
        futures = {
            pool.submit(contextvars.copy_context().run, analyzer.analyze, repo_path): analyzer
            for analyzer in analyzers
        }
        for fut in as_completed(futures):
            analyzer = futures[fut]
            try:
                score = fut.result()
            except Exception:
                log.exception("Analyzer %s failed", getattr(analyzer, "dimension", analyzer))
                raise
            by_dim[score.dimension] = score

    ordered = [by_dim[d] for d in _ANALYZER_ORDER if d in by_dim]
    # Any unexpected dimensions append deterministically.
    extras = sorted(d for d in by_dim if d not in _ANALYZER_ORDER)
    return ordered + [by_dim[d] for d in extras]


def _merge_check_findings(
    scores: list[DimensionScore],
    check_defs: list,
    repo_path: Path,
) -> tuple[list[DimensionScore], list[dict]]:
    """Merge data-driven check findings into existing analyzer scores.

    New findings from checks supplement (don't replace) analyzer findings.
    Findings are deduplicated by (category, description) so overlapping
    checks don't double-count. Every dimension covered by *any* check
    (legacy YAML or a `mode: detect` skill) gets a DimensionScore row --
    even one with zero failing checks (a clean 100/100), exactly like an
    analyzer already always does -- so a dimension whose only producer is
    checks (true today for every skill-only domain that gains a `mode:
    detect` skill, e.g. `chaos`/`cost`/`incident`, none of which have an
    analyzer at all) never silently disappears from report.scores just
    because every one of its checks passed. Returns the merged scores plus
    a pass/fail status row for every check that ran (for
    `check_results_out`).
    """
    extra, check_statuses = run_checks_by_dimension_with_status(check_defs, repo_path)

    score_map = {s.dimension: s for s in scores}
    original_dims = {s.dimension for s in scores}

    for dimension, findings in extra.items():
        existing = score_map.get(dimension)
        if existing is not None:
            existing_keys = {
                (f.category, f.description) for f in existing.findings
            }
            new_findings = [
                f for f in findings
                if (f.category, f.description) not in existing_keys
            ]
            if new_findings:
                merged = existing.findings + new_findings
                score_map[dimension] = DimensionScore(
                    dimension=dimension,
                    score=calculate_score(merged),
                    max_score=existing.max_score,
                    findings=merged,
                )
        else:
            # Dimension from checks not covered by any analyzer
            score_map[dimension] = DimensionScore(
                dimension=dimension,
                score=calculate_score(findings),
                max_score=100,
                findings=findings,
            )

    checked_dims = {s["dimension"] for s in check_statuses}
    for dimension in checked_dims:
        if dimension not in score_map:
            score_map[dimension] = DimensionScore(
                dimension=dimension, score=100, max_score=100, findings=[],
            )

    merged_scores = [score_map[s.dimension] for s in scores] + [
        score_map[d] for d in score_map if d not in original_dims
    ]
    return merged_scores, check_statuses


def _apply_suppressions(
    scores: list[DimensionScore],
    suppressions: set[str],
) -> list[DimensionScore]:
    """Remove findings whose source matches a suppressed check."""
    result = []
    for s in scores:
        filtered = [f for f in s.findings if f.source not in suppressions]
        if len(filtered) == len(s.findings):
            result.append(s)
        else:
            result.append(DimensionScore(
                dimension=s.dimension,
                score=calculate_score(filtered),
                max_score=s.max_score,
                findings=filtered,
            ))
    return result


def generate_remediation_plan(scores: list[DimensionScore]) -> list[RemediationItem]:
    all_findings = [
        (score.dimension, finding)
        for score in scores
        for finding in score.findings
    ]
    all_findings.sort(key=lambda x: x[1].severity.value)

    return [
        RemediationItem(
            priority=i,
            dimension=dimension,
            description=finding.description,
            estimated_effort=EFFORT_MAP.get(finding.severity, "30 agent-minutes"),
            agent_responsible=AGENT_MAP.get(dimension, "Unknown Agent"),
            category=finding.category,
        )
        for i, (dimension, finding) in enumerate(all_findings, start=1)
    ]


def _detect_architecture(
    repo_path: Path,
    snapshot: RepoSnapshot | None = None,
) -> ArchitectureInfo:
    service_count = 1
    has_api = False
    api_style = None
    auth_mechanism = None

    code_exts = {".py", ".go", ".java", ".js", ".ts"}

    def _scan_content(content: str) -> None:
        nonlocal has_api, api_style, auth_mechanism
        content_lower = content.lower()
        if any(kw in content_lower for kw in ["router", "endpoint", "handler", "@app.route", "@api", "http.handle"]):
            has_api = True
        if "grpc" in content_lower:
            api_style = "gRPC"
        elif "graphql" in content_lower:
            api_style = "GraphQL"
        elif has_api and api_style is None:
            api_style = "REST"
        if any(kw in content_lower for kw in ["oauth", "oidc", "saml", "jwt", "keycloak", "auth0"]):
            auth_mechanism = "SSO/OIDC"
        elif any(kw in content_lower for kw in ["basic auth", "basicauth", "session", "cookie"]):
            if auth_mechanism is None:
                auth_mechanism = "basic/session"

    def _compose_service_count(content: str) -> int:
        # Avoid double-counting a service that has both image: and build:.
        return max(content.count("image:"), content.count("build:"), 1)

    if snapshot is not None:
        for rel, content in snapshot.files.items():
            name = Path(rel).name
            if name.startswith("docker-compose") and rel.endswith((".yml", ".yaml")):
                service_count = max(service_count, _compose_service_count(content))
            if Path(rel).suffix.lower() not in code_exts:
                continue
            _scan_content(content)
    else:
        docker_composes = list(repo_path.glob("docker-compose*.y*ml"))
        if docker_composes:
            for dc in docker_composes:
                service_count = max(
                    service_count,
                    _compose_service_count(dc.read_text(errors="ignore")),
                )

        for fp in repo_path.rglob("*"):
            if not fp.is_file() or is_ignored(fp, repo_path):
                continue
            if fp.suffix.lower() not in code_exts:
                continue
            try:
                _scan_content(fp.read_text(errors="ignore"))
            except OSError:
                continue

    style = "monolith" if service_count <= 1 else "multi-service"
    if service_count > 3:
        style = "microservices"

    return ArchitectureInfo(
        service_count=max(1, service_count),
        architecture_style=style,
        has_api=has_api,
        api_style=api_style,
        external_dependencies=[],
        auth_mechanism=auth_mechanism,
    )


def _generate_summary(scores: list[DimensionScore], repo_name: str) -> str:
    avg = sum(s.score for s in scores) / len(scores) if scores else 0
    critical_count = sum(
        1 for s in scores for f in s.findings if f.severity == Severity.critical
    )
    high_count = sum(
        1 for s in scores for f in s.findings if f.severity == Severity.high
    )
    total_findings = sum(len(s.findings) for s in scores)

    return (
        f"{repo_name}: overall score {avg:.0f}/100 with {total_findings} findings "
        f"({critical_count} critical, {high_count} high). "
        f"Lowest dimensions: {', '.join(s.dimension for s in sorted(scores, key=lambda x: x.score)[:3])}."
    )
