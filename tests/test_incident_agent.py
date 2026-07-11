from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from agentit.agents.incident import IncidentAgent, IncidentResult
from agentit.models import (
    AssessmentReport,
    ArchitectureInfo,
    Database,
    DimensionScore,
    Finding,
    Framework,
    Language,
    Runtime,
    Severity,
    StackInfo,
)


def _make_report(
    *,
    repo_name: str = "test-app",
    languages: list[Language] | None = None,
    databases: list[Database] | None = None,
    criticality: str = "medium",
) -> AssessmentReport:
    if languages is None:
        languages = [Language(name="python", file_count=10, percentage=100.0)]
    return AssessmentReport(
        repo_url="https://github.com/org/test-app",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=languages,
            frameworks=[],
            databases=databases or [],
            runtimes=[],
            package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
        ),
        scores=[],
        criticality=criticality,
        summary="test summary",
        remediation_plan=[],
    )


class TestRunbook:
    def test_generates_runbook_with_stack_info(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
        )
        result = IncidentAgent(report, tmp_path / "out").run()

        runbooks = [f for f in result.files if f.path == "runbook.md"]
        assert len(runbooks) == 1

        content = runbooks[0].content
        assert "# Incident Response Runbook" in content
        assert "test-app" in content
        assert "kubectl get pods" in content
        assert "Triage Steps" in content
        assert "Recovery Procedures" in content
        assert "Escalation Contacts" in content
        assert (tmp_path / "out" / "runbook.md").exists()

    def test_runbook_includes_database_steps_when_db_detected(
        self, tmp_path: Path
    ) -> None:
        report = _make_report(
            databases=[Database(name="PostgreSQL", version="15")],
        )
        result = IncidentAgent(report, tmp_path / "out").run()

        runbooks = [f for f in result.files if f.path == "runbook.md"]
        assert len(runbooks) == 1

        content = runbooks[0].content
        assert "pg_isready" in content
        assert "PostgreSQL connections" in content
        assert "DB connection refused" in content
        assert "max_connections" in content


class TestPagerDutyConfig:
    def test_generates_pagerduty_config(self, tmp_path: Path) -> None:
        report = _make_report(criticality="critical")
        result = IncidentAgent(report, tmp_path / "out").run()

        pd_files = [f for f in result.files if f.path == "pagerduty-service.yaml"]
        assert len(pd_files) == 1

        doc = yaml.safe_load(pd_files[0].content)
        assert doc["kind"] == "ConfigMap"
        assert doc["metadata"]["name"] == "test-app-pagerduty"
        assert doc["data"]["service-name"] == "test-app"
        assert doc["data"]["urgency"] == "high"
        assert doc["data"]["escalation-timeout-seconds"] == "300"
        assert (tmp_path / "out" / "pagerduty-service.yaml").exists()

    def test_pagerduty_low_urgency_for_medium(self, tmp_path: Path) -> None:
        report = _make_report(criticality="medium")
        result = IncidentAgent(report, tmp_path / "out").run()

        pd_files = [f for f in result.files if f.path == "pagerduty-service.yaml"]
        doc = yaml.safe_load(pd_files[0].content)
        assert doc["data"]["urgency"] == "low"
        assert doc["data"]["escalation-timeout-seconds"] == "900"


class TestAlertManagerConfig:
    def test_generates_alertmanager_config(self, tmp_path: Path) -> None:
        report = _make_report()
        result = IncidentAgent(report, tmp_path / "out").run()

        am_files = [f for f in result.files if f.path == "alertmanager-config.yaml"]
        assert len(am_files) == 1

        doc = yaml.safe_load(am_files[0].content)
        assert doc["kind"] == "ConfigMap"
        assert doc["metadata"]["name"] == "test-app-alertmanager-routes"

        routes_yaml = yaml.safe_load(doc["data"]["routes.yaml"])
        route = routes_yaml["route"]
        assert route["group_wait"] == "30s"
        assert route["group_interval"] == "5m"

        sub_routes = route["routes"]
        receivers = {r["receiver"] for r in sub_routes}
        assert receivers == {"pagerduty", "slack", "email"}

        # critical -> pagerduty
        critical_route = [r for r in sub_routes if r["match"]["severity"] == "critical"]
        assert critical_route[0]["receiver"] == "pagerduty"

        # high -> slack
        high_route = [r for r in sub_routes if r["match"]["severity"] == "high"]
        assert high_route[0]["receiver"] == "slack"

        # medium -> email
        medium_route = [r for r in sub_routes if r["match"]["severity"] == "medium"]
        assert medium_route[0]["receiver"] == "email"

        assert (tmp_path / "out" / "alertmanager-config.yaml").exists()


class TestIncidentResult:
    def test_summary_count(self, tmp_path: Path) -> None:
        report = _make_report()
        result = IncidentAgent(report, tmp_path / "out").run()
        assert result.summary == "Generated 3 incident response artifacts."
