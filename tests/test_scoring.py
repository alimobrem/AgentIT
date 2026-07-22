"""Unit tests for shared scoring bands and estimated fix impact."""
from __future__ import annotations

from datetime import datetime, timezone

from agentit.models import (
    ArchitectureInfo, AssessmentReport, DimensionScore, Finding, Severity, StackInfo,
)
from agentit.scoring import (
    SCORE_GOOD, SCORE_OK, estimate_finding_overall_delta, letter_grade,
    score_band, score_text_class, top_fix_impacts,
)


def _report(*dim_findings: tuple[str, list[Finding]]) -> AssessmentReport:
    scores = []
    for dim, findings in dim_findings:
        from agentit.analyzers.base import calculate_score
        scores.append(DimensionScore(
            dimension=dim, score=calculate_score(findings), max_score=100, findings=findings,
        ))
    return AssessmentReport(
        repo_url="https://github.com/t/r",
        repo_name="r",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(languages=[], frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=False,
            api_style=None, external_dependencies=[], auth_mechanism=None,
        ),
        scores=scores,
        criticality="medium",
        summary="test",
        remediation_plan=[],
    )


def test_score_bands_and_letter():
    assert score_band(SCORE_GOOD + 1) == "good"
    assert score_band(SCORE_OK) == "ok"
    assert score_band(SCORE_OK - 1) == "poor"
    assert score_text_class(90) == "text-success"
    assert letter_grade(95) == "A"
    assert letter_grade(50) == "D"


def test_top_fix_impacts_ranks_by_delta():
    crit = Finding(
        category="container", severity=Severity.critical,
        description="no non-root", recommendation="runAsNonRoot",
    )
    low = Finding(
        category="logging", severity=Severity.low,
        description="no structured logs", recommendation="add logging",
    )
    report = _report(
        ("security", [crit]),
        ("observability", [low]),
    )
    top = top_fix_impacts(report, remediable_categories={"container", "logging"}, limit=3)
    assert top
    assert top[0]["category"] == "container"
    assert top[0]["estimated_delta"] > 0
    delta = estimate_finding_overall_delta(report, crit, "security")
    assert delta == top[0]["estimated_delta"]
