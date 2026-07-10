import json
from datetime import datetime, timezone

from agentit.models import (
    AssessmentReport, ArchitectureInfo, DimensionScore, Finding, Severity,
    StackInfo, Language, RemediationItem,
)
from agentit.reporter import render_json_report, render_terminal_report


def _make_report() -> AssessmentReport:
    return AssessmentReport(
        repo_url="https://github.com/test/app",
        repo_name="app",
        assessed_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="go", version="1.22", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=["go mod"],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith",
            has_api=True, api_style="REST",
            external_dependencies=[], auth_mechanism=None,
        ),
        scores=[
            DimensionScore(dimension="security", score=18, max_score=100, findings=[
                Finding(category="secrets", severity=Severity.critical,
                        description="Hardcoded password", recommendation="Use Vault"),
            ]),
            DimensionScore(dimension="observability", score=5, max_score=100, findings=[]),
        ],
        criticality="high",
        summary="",
        remediation_plan=[
            RemediationItem(priority=1, dimension="security",
                            description="Migrate secrets", estimated_effort="1 agent-hour",
                            agent_responsible="Security Hardening Agent"),
        ],
    )


def test_render_json_report_is_valid_json():
    report = _make_report()
    json_str = render_json_report(report)
    parsed = json.loads(json_str)
    assert parsed["repo_url"] == "https://github.com/test/app"
    assert len(parsed["scores"]) == 2


def test_render_terminal_report_contains_key_info():
    report = _make_report()
    output = render_terminal_report(report)
    assert "app" in output.lower()
    assert "security" in output.lower()
    assert "18" in output
