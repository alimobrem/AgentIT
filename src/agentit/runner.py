from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agentit.analyzers.cicd import CICDAnalyzer
from agentit.analyzers.compliance import ComplianceAnalyzer
from agentit.analyzers.data_governance import DataGovernanceAnalyzer
from agentit.analyzers.ha_dr import HADRAnalyzer
from agentit.analyzers.infrastructure import InfrastructureAnalyzer
from agentit.analyzers.observability import ObservabilityAnalyzer
from agentit.analyzers.security import SecurityAnalyzer
from agentit.analyzers.stack_detector import StackDetector
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


def run_assessment(
    repo_path: Path,
    repo_url: str,
    criticality: str = "medium",
) -> AssessmentReport:
    detector = StackDetector()
    stack = detector.detect(repo_path)

    architecture = _detect_architecture(repo_path, stack)

    analyzers = [
        SecurityAnalyzer(),
        ObservabilityAnalyzer(),
        CICDAnalyzer(),
        InfrastructureAnalyzer(),
        ComplianceAnalyzer(),
        DataGovernanceAnalyzer(),
        HADRAnalyzer(),
    ]

    scores = [analyzer.analyze(repo_path) for analyzer in analyzers]
    remediation_plan = generate_remediation_plan(scores)

    repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    return AssessmentReport(
        repo_url=repo_url,
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=stack,
        architecture=architecture,
        scores=scores,
        criticality=criticality,
        summary=_generate_summary(scores, stack, repo_name),
        remediation_plan=remediation_plan,
    )


def generate_remediation_plan(scores: list[DimensionScore]) -> list[RemediationItem]:
    items: list[RemediationItem] = []
    all_findings = []

    for score in scores:
        for finding in score.findings:
            all_findings.append((score.dimension, finding))

    all_findings.sort(key=lambda x: x[1].severity.value)

    for priority, (dimension, finding) in enumerate(all_findings, start=1):
        items.append(RemediationItem(
            priority=priority,
            dimension=dimension,
            description=finding.description,
            estimated_effort=EFFORT_MAP.get(finding.severity, "30 agent-minutes"),
            agent_responsible=AGENT_MAP.get(dimension, "Unknown Agent"),
        ))

    return items


def _detect_architecture(repo_path: Path, stack: object) -> ArchitectureInfo:
    service_count = 0
    has_api = False
    api_style = None
    auth_mechanism = None
    external_deps: list[str] = []

    docker_composes = list(repo_path.glob("docker-compose*.y*ml"))
    if docker_composes:
        for dc in docker_composes:
            content = dc.read_text(errors="ignore")
            service_count = content.count("image:") + content.count("build:")
    else:
        service_count = 1

    for fp in repo_path.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in {".py", ".go", ".java", ".js", ".ts"}:
            continue
        try:
            content = fp.read_text(errors="ignore")
        except OSError:
            continue
        if any(kw in content.lower() for kw in ["router", "endpoint", "handler", "@app.route", "@api", "http.handle"]):
            has_api = True
        if "grpc" in content.lower():
            api_style = "gRPC"
        elif "graphql" in content.lower():
            api_style = "GraphQL"
        elif has_api and api_style is None:
            api_style = "REST"
        if any(kw in content.lower() for kw in ["oauth", "oidc", "saml", "jwt", "keycloak", "auth0"]):
            auth_mechanism = "SSO/OIDC"
        elif any(kw in content.lower() for kw in ["basic auth", "basicauth", "session", "cookie"]):
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
        external_dependencies=external_deps,
        auth_mechanism=auth_mechanism,
    )


def _generate_summary(scores: list[DimensionScore], stack: object, repo_name: str) -> str:
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
