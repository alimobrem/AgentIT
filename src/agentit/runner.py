from __future__ import annotations

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
from agentit.analyzers.stack_detector import StackDetector
from agentit.check_engine import load_checks, run_checks_by_dimension
from agentit.models import (
    ArchitectureInfo, AssessmentReport, DimensionScore, RemediationItem, Severity,
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


def run_assessment(
    repo_path: Path,
    repo_url: str,
    criticality: str = "medium",
    llm_client: object | None = None,
    infra_repo_url: str | None = None,
    checks_dir: Path | None = None,
) -> AssessmentReport:
    detector = StackDetector()
    stack = detector.detect(repo_path)

    architecture = _detect_architecture(repo_path)

    analyzers = [
        SecurityAnalyzer(llm_client=llm_client),
        ObservabilityAnalyzer(),
        CICDAnalyzer(),
        InfrastructureAnalyzer(),
        ComplianceAnalyzer(),
        DataGovernanceAnalyzer(),
        HADRAnalyzer(),
    ]

    scores = [analyzer.analyze(repo_path) for analyzer in analyzers]

    # Run data-driven checks and merge findings into dimension scores
    resolved_checks_dir = checks_dir if checks_dir is not None else _default_checks_dir()
    check_defs = load_checks(resolved_checks_dir)
    if check_defs:
        scores = _merge_check_findings(scores, check_defs, repo_path)

    remediation_plan = generate_remediation_plan(scores)

    repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    summary = _generate_summary(scores, repo_name)
    if llm_client is not None:
        llm_summary = llm_client.summarize_architecture(
            stack.model_dump(),
            [str(p.relative_to(repo_path)) for p in repo_path.rglob("*") if p.is_file() and not is_ignored(p, repo_path)],
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
    )


def _merge_check_findings(
    scores: list[DimensionScore],
    check_defs: list,
    repo_path: Path,
) -> list[DimensionScore]:
    """Merge data-driven check findings into existing analyzer scores.

    New findings from checks supplement (don't replace) analyzer findings.
    Findings are deduplicated by (category, description) so overlapping
    checks don't double-count.
    """
    extra = run_checks_by_dimension(check_defs, repo_path)
    if not extra:
        return scores

    score_map = {s.dimension: s for s in scores}

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

    return [score_map[s.dimension] for s in scores] + [
        score_map[d] for d in score_map if d not in {s.dimension for s in scores}
    ]


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
        )
        for i, (dimension, finding) in enumerate(all_findings, start=1)
    ]


def _detect_architecture(repo_path: Path) -> ArchitectureInfo:
    service_count = 1
    has_api = False
    api_style = None
    auth_mechanism = None

    docker_composes = list(repo_path.glob("docker-compose*.y*ml"))
    if docker_composes:
        for dc in docker_composes:
            content = dc.read_text(errors="ignore")
            service_count = content.count("image:") + content.count("build:")

    for fp in repo_path.rglob("*"):
        if not fp.is_file() or is_ignored(fp, repo_path):
            continue
        if fp.suffix.lower() not in {".py", ".go", ".java", ".js", ".ts"}:
            continue
        try:
            content = fp.read_text(errors="ignore")
        except OSError:
            continue
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
