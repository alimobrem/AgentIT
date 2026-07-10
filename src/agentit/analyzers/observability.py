from __future__ import annotations

import re
from pathlib import Path

from agentit.models import DimensionScore, Finding, Severity

OTEL_PATTERNS = [
    "opentelemetry", "otel", "go.opentelemetry.io",
    "io.opentelemetry", "@opentelemetry",
]
METRICS_PATTERNS = ["prometheus", "ServiceMonitor", "PodMonitor", "metrics", "statsd"]
LOGGING_PATTERNS = ["structlog", "zap", "logrus", "winston", "pino", "slog"]
TRACING_PATTERNS = ["jaeger", "zipkin", "tempo", "trace", "opentracing"]
DASHBOARD_PATTERNS = ["grafana", "dashboard"]
ALERTING_PATTERNS = ["PrometheusRule", "alertmanager", "alerting", "pagerduty", "opsgenie"]

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "dist", "build", "target"}


class ObservabilityAnalyzer:
    dimension = "observability"

    def analyze(self, repo_path: Path) -> DimensionScore:
        findings: list[Finding] = []
        checks = {
            "instrumentation": (OTEL_PATTERNS, "No OpenTelemetry or metrics instrumentation detected", "Add OpenTelemetry SDK for auto-instrumentation"),
            "metrics": (METRICS_PATTERNS, "No Prometheus metrics or ServiceMonitor found", "Create ServiceMonitor for Prometheus scraping"),
            "logging": (LOGGING_PATTERNS, "No structured logging library detected", "Add structured JSON logging (e.g., structlog for Python, zap for Go)"),
            "tracing": (TRACING_PATTERNS, "No distributed tracing detected", "Add OpenTelemetry tracing with Tempo exporter"),
            "dashboards": (DASHBOARD_PATTERNS, "No Grafana dashboards found", "Create Grafana dashboards for RED metrics"),
            "alerting": (ALERTING_PATTERNS, "No alerting rules or integrations found", "Define PrometheusRule alerting rules for SLO-based alerts"),
        }

        all_content = self._read_all_text(repo_path)

        for category, (patterns, description, recommendation) in checks.items():
            if not any(p.lower() in all_content.lower() for p in patterns):
                findings.append(Finding(
                    category=category,
                    severity=Severity.high if category in ("instrumentation", "metrics") else Severity.medium,
                    description=description,
                    recommendation=recommendation,
                ))

        score = 100
        for f in findings:
            score -= 18 if f.severity == Severity.high else 12
        return DimensionScore(dimension="observability", score=max(0, score), max_score=100, findings=findings)

    def _read_all_text(self, repo_path: Path) -> str:
        parts: list[str] = []
        for fp in repo_path.rglob("*"):
            if not fp.is_file() or any(d in fp.relative_to(repo_path).parts for d in IGNORED_DIRS):
                continue
            if fp.suffix.lower() in {".py", ".go", ".java", ".js", ".ts", ".yaml", ".yml", ".json", ".toml", ".xml", ".gradle"}:
                try:
                    parts.append(fp.read_text(errors="ignore"))
                except OSError:
                    continue
        return "\n".join(parts)
