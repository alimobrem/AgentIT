from __future__ import annotations

import json
from pathlib import Path

import yaml
import pytest

from conftest import make_report

from agentit.agents.observability import ObservabilityAgent, ObservabilityResult
from agentit.models import (
    DimensionScore,
    Finding,
    Severity,
)


def _score_with_finding(dimension: str, category: str, desc: str) -> DimensionScore:
    return DimensionScore(
        dimension=dimension,
        score=30,
        max_score=100,
        findings=[
            Finding(
                category=category,
                severity=Severity.high,
                description=desc,
                recommendation="fix it",
            ),
        ],
    )


class TestServiceMonitor:
    def test_generates_service_monitor(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("observability", "metrics", "No metrics endpoint")],
        )
        result = ObservabilityAgent(report, tmp_path / "out").run()

        sm_files = [f for f in result.files if f.path == "servicemonitor.yaml"]
        assert len(sm_files) == 1

        doc = yaml.safe_load(sm_files[0].content)
        assert doc["kind"] == "ServiceMonitor"
        assert doc["metadata"]["name"] == "test-app-monitor"
        assert doc["spec"]["endpoints"][0]["targetPort"] == 8080
        assert (tmp_path / "out" / "servicemonitor.yaml").exists()

    def test_skips_service_monitor_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ObservabilityAgent(report, tmp_path / "out").run()
        assert not any(f.path == "servicemonitor.yaml" for f in result.files)


class TestGrafanaDashboard:
    def test_generates_grafana_dashboard_configmap(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ObservabilityAgent(report, tmp_path / "out").run()

        cm_files = [f for f in result.files if f.path == "grafana-dashboard-cm.yaml"]
        assert len(cm_files) == 1

        import yaml
        cm = yaml.safe_load(cm_files[0].content)
        assert cm["kind"] == "ConfigMap"
        assert cm["metadata"]["labels"]["grafana_dashboard"] == "1"
        dashboard = json.loads(cm["data"]["test-app-dashboard.json"])
        assert dashboard["title"] == "test-app"
        assert len(dashboard["panels"]) == 4
        panel_titles = {p["title"] for p in dashboard["panels"]}
        assert "Requests / sec" in panel_titles
        assert "Error Rate %" in panel_titles
        assert (tmp_path / "out" / "grafana-dashboard-cm.yaml").exists()


class TestAlertingRules:
    def test_generates_alerting_rules(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ObservabilityAgent(report, tmp_path / "out").run()

        ar_files = [f for f in result.files if f.path == "alerting-rules.yaml"]
        assert len(ar_files) == 1

        doc = yaml.safe_load(ar_files[0].content)
        assert doc["kind"] == "PrometheusRule"
        rules = doc["spec"]["groups"][0]["rules"]
        alert_names = {r["alert"] for r in rules}
        assert alert_names == {"HighErrorRate", "HighLatency", "PodCrashLooping"}
        assert (tmp_path / "out" / "alerting-rules.yaml").exists()


class TestOtelCollector:
    def test_generates_otel_collector(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("observability", "tracing", "No tracing configured")],
        )
        result = ObservabilityAgent(report, tmp_path / "out").run()

        otel_files = [f for f in result.files if f.path == "otel-collector.yaml"]
        assert len(otel_files) == 1

        doc = yaml.safe_load(otel_files[0].content)
        assert doc["kind"] == "OpenTelemetryCollector"
        receivers = doc["spec"]["config"]["receivers"]
        assert "otlp" in receivers
        assert "grpc" in receivers["otlp"]["protocols"]
        assert "http" in receivers["otlp"]["protocols"]
        assert (tmp_path / "out" / "otel-collector.yaml").exists()

    def test_skips_otel_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = ObservabilityAgent(report, tmp_path / "out").run()
        assert not any(f.path == "otel-collector.yaml" for f in result.files)
