import pytest
from datetime import datetime, timezone
from agentit.models import (
    AssessmentReport, DimensionScore, Finding, Severity,
    StackInfo, Language, Framework, Database, Runtime,
    ArchitectureInfo, RemediationItem,
)


def test_severity_ordering():
    assert Severity.critical.value < Severity.high.value
    assert Severity.high.value < Severity.medium.value
    assert Severity.medium.value < Severity.low.value
    assert Severity.low.value < Severity.info.value


def test_dimension_score_clamps_to_0_100():
    score = DimensionScore(
        dimension="security",
        score=150,
        max_score=100,
        findings=[],
    )
    assert score.score == 100

    score_neg = DimensionScore(
        dimension="security",
        score=-10,
        max_score=100,
        findings=[],
    )
    assert score_neg.score == 0


def test_finding_requires_severity():
    finding = Finding(
        category="secrets",
        severity=Severity.critical,
        description="Hardcoded password in config.yaml",
        file_path="config.yaml",
        recommendation="Migrate to ExternalSecrets",
    )
    assert finding.severity == Severity.critical


def test_stack_info_minimal():
    stack = StackInfo(
        languages=[Language(name="python", version="3.12", file_count=42, percentage=85.0)],
        frameworks=[Framework(name="flask", version="3.0", language="python")],
        databases=[Database(name="postgresql", version="15", connection_method="psycopg2")],
        runtimes=[Runtime(name="cpython", version="3.12")],
        package_managers=["pip"],
    )
    assert stack.languages[0].name == "python"
    assert len(stack.frameworks) == 1


def test_assessment_report_overall_score_is_weighted_v2():
    """Score v2: criticality-weighted mean (high: security 1.4, observability 1.0)."""
    scores = [
        DimensionScore(dimension="security", score=20, max_score=100, findings=[]),
        DimensionScore(dimension="observability", score=80, max_score=100, findings=[]),
    ]
    report = AssessmentReport(
        repo_url="https://github.com/test/repo",
        repo_name="repo",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(languages=[], frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=scores,
        criticality="high",
        summary="",
        remediation_plan=[],
    )
    # (20*1.4 + 80*1.0) / (1.4+1.0) = 45.0
    assert report.score_version == 2
    assert report.overall_score == 45.0


def test_assessment_report_overall_score_v1_is_equal_mean():
    scores = [
        DimensionScore(dimension="security", score=20, max_score=100, findings=[]),
        DimensionScore(dimension="observability", score=80, max_score=100, findings=[]),
    ]
    report = AssessmentReport(
        repo_url="https://github.com/test/repo",
        repo_name="repo",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(languages=[], frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=scores,
        criticality="high",
        summary="",
        remediation_plan=[],
        score_version=1,
    )
    assert report.overall_score == 50.0


def test_assessment_report_json_roundtrip():
    report = AssessmentReport(
        repo_url="https://github.com/test/repo",
        repo_name="repo",
        assessed_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        stack=StackInfo(languages=[], frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=False,
            api_style=None,
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=[],
        criticality="low",
        summary="Test",
        remediation_plan=[],
    )
    json_str = report.model_dump_json()
    restored = AssessmentReport.model_validate_json(json_str)
    assert restored.repo_url == report.repo_url
    assert restored.assessed_at == report.assessed_at


def test_infra_repo_url_defaults_to_none():
    from conftest import make_report
    report = make_report()
    assert report.infra_repo_url is None


def test_infra_repo_url_roundtrip():
    from conftest import make_report
    report = make_report()
    report.infra_repo_url = "https://github.com/org/gitops-infra"
    json_str = report.model_dump_json()
    restored = AssessmentReport.model_validate_json(json_str)
    assert restored.infra_repo_url == "https://github.com/org/gitops-infra"
