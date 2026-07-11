from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from conftest import make_report

from agentit.agents.cost import CostOptimizationAgent, CostResult


class TestCostReport:
    def test_generates_cost_report(self, tmp_path: Path) -> None:
        report = make_report()
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        cr = [f for f in result.files if f.path == "cost-report.md"]
        assert len(cr) == 1

        content = cr[0].content
        assert "python" in content
        assert "small" in content
        assert "$15-30" in content
        assert "Resource Right-Sizing" in content
        assert "Idle Resource Detection" in content
        assert "Reserved Capacity" in content
        assert (tmp_path / "out" / "cost-report.md").exists()


class TestVPA:
    def test_generates_vpa_auto_for_non_critical(self, tmp_path: Path) -> None:
        report = make_report(criticality="medium")
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        vpa = [f for f in result.files if f.path == "resource-recommendations.yaml"]
        assert len(vpa) == 1

        doc = yaml.safe_load(vpa[0].content)
        assert doc["kind"] == "VerticalPodAutoscaler"
        assert doc["spec"]["updatePolicy"]["updateMode"] == "Auto"
        assert doc["spec"]["targetRef"]["kind"] == "Deployment"
        assert (tmp_path / "out" / "resource-recommendations.yaml").exists()

    def test_generates_vpa_off_for_critical(self, tmp_path: Path) -> None:
        report = make_report(criticality="critical")
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        vpa = [f for f in result.files if f.path == "resource-recommendations.yaml"]
        assert len(vpa) == 1

        doc = yaml.safe_load(vpa[0].content)
        assert doc["spec"]["updatePolicy"]["updateMode"] == "Off"

    def test_generates_vpa_off_for_high(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        vpa = [f for f in result.files if f.path == "resource-recommendations.yaml"]
        doc = yaml.safe_load(vpa[0].content)
        assert doc["spec"]["updatePolicy"]["updateMode"] == "Off"


class TestCostLabels:
    def test_generates_cost_labels(self, tmp_path: Path) -> None:
        report = make_report()
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        cl = [f for f in result.files if f.path == "cost-labels.yaml"]
        assert len(cl) == 1

        doc = yaml.safe_load(cl[0].content)
        assert doc["kind"] == "ConfigMap"
        assert doc["data"]["cost-center"] == "engineering"
        assert doc["data"]["team"] == "test-app"
        assert doc["data"]["environment"] == "development"
        assert doc["data"]["app-tier"] == "small"
        assert (tmp_path / "out" / "cost-labels.yaml").exists()

    def test_labels_production_for_critical(self, tmp_path: Path) -> None:
        report = make_report(criticality="critical")
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        cl = [f for f in result.files if f.path == "cost-labels.yaml"]
        doc = yaml.safe_load(cl[0].content)
        assert doc["data"]["environment"] == "production"


class TestTierEstimation:
    def test_large_tier_for_many_services(self, tmp_path: Path) -> None:
        report = make_report()
        report.architecture.service_count = 5
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        cr = [f for f in result.files if f.path == "cost-report.md"]
        assert "large" in cr[0].content

    def test_medium_tier_for_two_services(self, tmp_path: Path) -> None:
        report = make_report()
        report.architecture.service_count = 2
        result = CostOptimizationAgent(report, tmp_path / "out").run()

        cr = [f for f in result.files if f.path == "cost-report.md"]
        assert "medium" in cr[0].content


class TestOutputDir:
    def test_output_dir_created(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "deep" / "dir"
        assert not out.exists()
        report = make_report()
        CostOptimizationAgent(report, out).run()
        assert out.exists()
        assert out.is_dir()


class TestSummary:
    def test_result_summary(self, tmp_path: Path) -> None:
        report = make_report()
        result = CostOptimizationAgent(report, tmp_path / "out").run()
        assert result.summary == "Generated 4 cost optimization artifacts."
        assert len(result.files) == 4


class TestCostCronJob:
    def test_generates_cost_cronjob(self, tmp_path: Path) -> None:
        report = make_report()
        result = CostOptimizationAgent(report, tmp_path / "out").run()
        cw = [f for f in result.files if f.path == "cost-cronjob.yaml"]
        assert len(cw) == 1

        doc = yaml.safe_load(cw[0].content)
        assert doc["kind"] == "CronJob"
        assert doc["apiVersion"] == "batch/v1"
        assert doc["spec"]["schedule"] == "0 4 * * 1"
        assert doc["spec"]["concurrencyPolicy"] == "Replace"
