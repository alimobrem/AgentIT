import json

from agentit.models import DimensionScore, Finding, Severity
from agentit.reporter import render_json_report, render_terminal_report
from conftest import make_report


def test_render_json_report_is_valid_json():
    report = make_report(
        repo_name="app",
        repo_url="https://github.com/test/app",
        scores=[
            DimensionScore(dimension="security", score=18, max_score=100, findings=[
                Finding(category="secrets", severity=Severity.critical,
                        description="Hardcoded password", recommendation="Use Vault"),
            ]),
            DimensionScore(dimension="observability", score=5, max_score=100, findings=[]),
        ],
        criticality="high",
        summary="",
    )
    json_str = render_json_report(report)
    parsed = json.loads(json_str)
    assert parsed["repo_url"] == "https://github.com/test/app"
    assert len(parsed["scores"]) == 2


def test_render_terminal_report_contains_key_info():
    report = make_report(
        repo_name="app",
        repo_url="https://github.com/test/app",
        scores=[
            DimensionScore(dimension="security", score=18, max_score=100, findings=[
                Finding(category="secrets", severity=Severity.critical,
                        description="Hardcoded password", recommendation="Use Vault"),
            ]),
            DimensionScore(dimension="observability", score=5, max_score=100, findings=[]),
        ],
        criticality="high",
        summary="",
    )
    output = render_terminal_report(report)
    assert "app" in output.lower()
    assert "security" in output.lower()
    assert "18" in output
