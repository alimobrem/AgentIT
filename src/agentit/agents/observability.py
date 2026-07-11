from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport


class ObservabilityResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} observability manifest{'s' if count != 1 else ''}."
        )


class ObservabilityAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> ObservabilityResult:
        """Generate observability manifests based on assessment findings."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.extend(self._generate_service_monitor())
        generated.extend(self._generate_grafana_dashboard())
        generated.extend(self._generate_alerting_rules())
        generated.extend(self._generate_otel_collector())

        return ObservabilityResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _findings_for(self, *categories: str) -> list[str]:
        """Return descriptions of findings whose category contains any keyword."""
        hits: list[str] = []
        for score in self.report.scores:
            for f in score.findings:
                if any(kw in f.category.lower() for kw in categories):
                    hits.append(f.description)
        return hits

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_service_monitor(self) -> list[GeneratedFile]:
        hits = self._findings_for("metrics")
        if not hits:
            return []

        name = self._name
        doc = {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "ServiceMonitor",
            "metadata": {
                "name": f"{name}-monitor",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "selector": {
                    "matchLabels": {"app": name},
                },
                "endpoints": [
                    {
                        "port": "http",
                        "targetPort": 8080,
                        "path": "/metrics",
                        "interval": "30s",
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("servicemonitor.yaml", content)

        return [
            GeneratedFile(
                path="servicemonitor.yaml",
                content=content,
                description=f"Prometheus ServiceMonitor for {name} on port 8080.",
                finding_addressed="; ".join(hits),
            ),
        ]

    def _generate_grafana_dashboard(self) -> list[GeneratedFile]:
        name = self._name
        dashboard = {
            "annotations": {"list": []},
            "editable": True,
            "fiscalYearStartMonth": 0,
            "graphTooltip": 0,
            "id": None,
            "links": [],
            "panels": [
                {
                    "title": "Requests / sec",
                    "type": "timeseries",
                    "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
                    "targets": [
                        {
                            "expr": f'sum(rate(http_requests_total{{app="{name}"}}[5m]))',
                            "legendFormat": "req/s",
                        },
                    ],
                },
                {
                    "title": "Error Rate %",
                    "type": "timeseries",
                    "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
                    "targets": [
                        {
                            "expr": (
                                f'sum(rate(http_requests_total{{app="{name}",code=~"5.."}}[5m]))'
                                f" / sum(rate(http_requests_total{{app=\"{name}\"}}[5m])) * 100"
                            ),
                            "legendFormat": "error %",
                        },
                    ],
                },
                {
                    "title": "P99 Latency",
                    "type": "timeseries",
                    "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
                    "targets": [
                        {
                            "expr": f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{app="{name}"}}[5m])) by (le))',
                            "legendFormat": "p99",
                        },
                    ],
                },
                {
                    "title": "Pod Restarts",
                    "type": "timeseries",
                    "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
                    "targets": [
                        {
                            "expr": f'sum(kube_pod_container_status_restarts_total{{pod=~"{name}-.*"}}) by (pod)',
                            "legendFormat": "{{{{pod}}}}",
                        },
                    ],
                },
            ],
            "schemaVersion": 39,
            "tags": ["generated", name],
            "templating": {"list": []},
            "time": {"from": "now-1h", "to": "now"},
            "title": name,
            "uid": f"{name}-dashboard",
        }

        dashboard_json = json.dumps(dashboard, indent=2)

        configmap: dict = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-grafana-dashboard",
                "labels": {
                    "app.kubernetes.io/name": name,
                    "grafana_dashboard": "1",
                },
            },
            "data": {
                f"{name}-dashboard.json": dashboard_json,
            },
        }
        content = yaml.dump(configmap, default_flow_style=False, sort_keys=False)
        self._write("grafana-dashboard-cm.yaml", content)

        return [
            GeneratedFile(
                path="grafana-dashboard-cm.yaml",
                content=content,
                description=f"ConfigMap with Grafana RED metrics dashboard for {name} (auto-imported by Grafana sidecar via grafana_dashboard=1 label).",
                finding_addressed="Observability baseline: RED metrics dashboard.",
            ),
        ]

    def _generate_alerting_rules(self) -> list[GeneratedFile]:
        name = self._name
        doc = {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "PrometheusRule",
            "metadata": {
                "name": f"{name}-alerts",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "groups": [
                    {
                        "name": f"{name}.rules",
                        "rules": [
                            {
                                "alert": "HighErrorRate",
                                "expr": (
                                    f'sum(rate(http_requests_total{{app="{name}",code=~"5.."}}[5m]))'
                                    f' / sum(rate(http_requests_total{{app="{name}"}}[5m])) > 0.05'
                                ),
                                "for": "5m",
                                "labels": {"severity": "critical"},
                                "annotations": {
                                    "summary": f"High error rate on {name}",
                                    "description": "Error rate exceeds 5% for 5 minutes.",
                                },
                            },
                            {
                                "alert": "HighLatency",
                                "expr": (
                                    f"histogram_quantile(0.99, "
                                    f'sum(rate(http_request_duration_seconds_bucket{{app="{name}"}}[5m])) by (le)) > 1'
                                ),
                                "for": "5m",
                                "labels": {"severity": "warning"},
                                "annotations": {
                                    "summary": f"High p99 latency on {name}",
                                    "description": "P99 latency exceeds 1s for 5 minutes.",
                                },
                            },
                            {
                                "alert": "PodCrashLooping",
                                "expr": (
                                    f'increase(kube_pod_container_status_restarts_total{{pod=~"{name}-.*"}}[10m]) > 3'
                                ),
                                "for": "0m",
                                "labels": {"severity": "critical"},
                                "annotations": {
                                    "summary": f"Pod crash looping for {name}",
                                    "description": "Pod has restarted more than 3 times in 10 minutes.",
                                },
                            },
                        ],
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("alerting-rules.yaml", content)

        return [
            GeneratedFile(
                path="alerting-rules.yaml",
                content=content,
                description=f"PrometheusRule with HighErrorRate, HighLatency, PodCrashLooping alerts for {name}.",
                finding_addressed="Observability baseline: alerting rules.",
            ),
        ]

    def _generate_otel_collector(self) -> list[GeneratedFile]:
        hits = self._findings_for("tracing")
        if not hits:
            return []

        name = self._name
        doc = {
            "apiVersion": "opentelemetry.io/v1beta1",
            "kind": "OpenTelemetryCollector",
            "metadata": {
                "name": f"{name}-otel",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "mode": "sidecar",
                "config": {
                    "receivers": {
                        "otlp": {
                            "protocols": {
                                "grpc": {"endpoint": "0.0.0.0:4317"},
                                "http": {"endpoint": "0.0.0.0:4318"},
                            },
                        },
                    },
                    "exporters": {
                        "otlp": {
                            "endpoint": "tempo.observability.svc.cluster.local:4317",
                            "tls": {"insecure": True},
                        },
                    },
                    "service": {
                        "pipelines": {
                            "traces": {
                                "receivers": ["otlp"],
                                "exporters": ["otlp"],
                            },
                        },
                    },
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("otel-collector.yaml", content)

        return [
            GeneratedFile(
                path="otel-collector.yaml",
                content=content,
                description=f"OpenTelemetryCollector CR for {name} with OTLP receivers and Tempo exporter.",
                finding_addressed="; ".join(hits),
            ),
        ]
