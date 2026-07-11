from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from conftest import make_report

from agentit.agents.retirement import RetirementAgent, RetirementResult
from agentit.models import Database


class TestDecommissionPlan:
    def test_generates_decommission_plan(self, tmp_path: Path) -> None:
        report = make_report()
        result = RetirementAgent(report, tmp_path / "out").run()

        plan = [f for f in result.files if f.path == "decommission-plan.md"]
        assert len(plan) == 1

        content = plan[0].content
        assert "Decommission Plan: test-app" in content
        assert "DNS / Route Cleanup" in content
        assert "Dependency Notification Checklist" in content
        assert "Resource Reclamation" in content
        assert "30-Day Sunset" in content
        assert (tmp_path / "out" / "decommission-plan.md").exists()

    def test_plan_includes_detected_databases(self, tmp_path: Path) -> None:
        report = make_report()
        report.stack.databases = [Database(name="PostgreSQL", version="15")]
        result = RetirementAgent(report, tmp_path / "out").run()

        plan = [f for f in result.files if f.path == "decommission-plan.md"]
        content = plan[0].content
        assert "PostgreSQL" in content
        assert "Back up **PostgreSQL** data" in content

    def test_plan_no_databases(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = RetirementAgent(report, tmp_path / "out").run()

        plan = [f for f in result.files if f.path == "decommission-plan.md"]
        content = plan[0].content
        assert "No databases detected" in content


class TestCleanupScript:
    def test_generates_cleanup_script(self, tmp_path: Path) -> None:
        report = make_report()
        result = RetirementAgent(report, tmp_path / "out").run()

        script = [f for f in result.files if f.path == "cleanup-script.sh"]
        assert len(script) == 1

        content = script[0].content
        assert "#!/usr/bin/env bash" in content
        assert "set -euo pipefail" in content
        assert "delete deployment" in content
        assert "delete service" in content
        assert "delete route" in content
        assert "delete configmap" in content
        assert "delete secret" in content
        assert "delete pvc" in content
        assert "--force" in content
        assert "delete namespace" in content
        assert (tmp_path / "out" / "cleanup-script.sh").exists()

    def test_cleanup_script_executable(self, tmp_path: Path) -> None:
        report = make_report()
        RetirementAgent(report, tmp_path / "out").run()
        script_path = tmp_path / "out" / "cleanup-script.sh"
        assert script_path.stat().st_mode & 0o755


class TestDataArchive:
    def test_generates_data_archive_for_postgres(self, tmp_path: Path) -> None:
        report = make_report()
        report.stack.databases = [Database(name="PostgreSQL", version="15")]
        result = RetirementAgent(report, tmp_path / "out").run()

        archive = [f for f in result.files if f.path == "data-archive-job.yaml"]
        assert len(archive) == 1

        doc = yaml.safe_load(archive[0].content)
        assert doc["kind"] == "Job"
        assert doc["metadata"]["name"] == "test-app-data-archive"
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert "pg_dump" in container["command"][-1]
        assert (tmp_path / "out" / "data-archive-job.yaml").exists()

    def test_skips_data_archive_without_database(self, tmp_path: Path) -> None:
        report = make_report()
        result = RetirementAgent(report, tmp_path / "out").run()

        archive = [f for f in result.files if f.path == "data-archive-job.yaml"]
        assert len(archive) == 0

    def test_skips_data_archive_for_non_postgres(self, tmp_path: Path) -> None:
        report = make_report()
        report.stack.databases = [Database(name="Redis", version="7")]
        result = RetirementAgent(report, tmp_path / "out").run()

        archive = [f for f in result.files if f.path == "data-archive-job.yaml"]
        assert len(archive) == 0


class TestRetirementResult:
    def test_summary_count(self, tmp_path: Path) -> None:
        report = make_report()
        result = RetirementAgent(report, tmp_path / "out").run()
        # Without postgres: decommission-plan.md + cleanup-script.sh = 2
        assert result.summary == "Generated 2 retirement artifacts."

    def test_summary_with_archive(self, tmp_path: Path) -> None:
        report = make_report()
        report.stack.databases = [Database(name="PostgreSQL")]
        result = RetirementAgent(report, tmp_path / "out").run()
        assert result.summary == "Generated 3 retirement artifacts."

    def test_output_dir_created(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "deep" / "dir"
        assert not out.exists()
        report = make_report()
        RetirementAgent(report, out).run()
        assert out.exists()
        assert out.is_dir()
