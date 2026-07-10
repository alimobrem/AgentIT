from pathlib import Path

from agentit.models import AssessmentReport
from agentit.runner import run_assessment


def test_run_assessment_produces_report(create_mock_repo):
    repo = create_mock_repo({
        "go.mod": "module github.com/test/app\n\ngo 1.22\n",
        "main.go": "package main\nfunc main() {}\n",
        "Dockerfile": "FROM golang:1.22\nCMD ['app']\n",
    })
    report = run_assessment(repo, repo_url="https://github.com/test/app", criticality="medium")
    assert isinstance(report, AssessmentReport)
    assert report.repo_url == "https://github.com/test/app"
    assert report.criticality == "medium"
    assert len(report.scores) == 7
    assert report.stack.languages[0].name == "go"
    assert 0 <= report.overall_score <= 100


def test_run_assessment_generates_remediation_plan(create_mock_repo):
    repo = create_mock_repo({"README.md": "# Empty"})
    report = run_assessment(repo, repo_url="https://github.com/test/empty", criticality="high")
    assert len(report.remediation_plan) > 0
    priorities = [item.priority for item in report.remediation_plan]
    assert priorities == sorted(priorities)
