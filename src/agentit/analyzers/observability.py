from __future__ import annotations

from pathlib import Path

from agentit.analyzers.base import calculate_score, iter_text_files
from agentit.models import DimensionScore, Finding, Severity

OTEL_PATTERNS = ["opentelemetry", "otel", "go.opentelemetry.io", "io.opentelemetry", "@opentelemetry"]
METRICS_PATTERNS = ["prometheus", "servicemonitor", "podmonitor", "metrics", "statsd"]
LOGGING_PATTERNS = ["structlog", "zap", "logrus", "winston", "pino", "slog"]
TRACING_PATTERNS = ["jaeger", "zipkin", "tempo", "trace", "opentracing"]
DASHBOARD_PATTERNS = ["grafana", "dashboard"]
ALERTING_PATTERNS = ["prometheusrule", "alertmanager", "alerting", "pagerduty", "opsgenie"]

CHECKS: dict[str, tuple[list[str], str, str]] = {
    "instrumentation": (OTEL_PATTERNS, "No OpenTelemetry or metrics instrumentation detected", "Add OpenTelemetry SDK for auto-instrumentation"),
    "metrics": (METRICS_PATTERNS, "No Prometheus metrics or ServiceMonitor found", "Create ServiceMonitor for Prometheus scraping"),
    "logging": (LOGGING_PATTERNS, "No structured logging library detected", "Add structured JSON logging (e.g., structlog for Python, zap for Go)"),
    "tracing": (TRACING_PATTERNS, "No distributed tracing detected", "Add OpenTelemetry tracing with Tempo exporter"),
    "dashboards": (DASHBOARD_PATTERNS, "No Grafana dashboards found", "Create Grafana dashboards for RED metrics"),
    "alerting": (ALERTING_PATTERNS, "No alerting rules or integrations found", "Define PrometheusRule alerting rules for SLO-based alerts"),
}


class ObservabilityAnalyzer:
    dimension = "observability"

    def analyze(self, repo_path: Path) -> DimensionScore:
        all_lower = "\n".join(content for _, content in iter_text_files(repo_path)).lower()

        findings: list[Finding] = []
        for category, (patterns, description, recommendation) in CHECKS.items():
            if not any(p in all_lower for p in patterns):
                findings.append(Finding(
                    category=category,
                    severity=Severity.high if category in ("instrumentation", "metrics") else Severity.medium,
                    description=description,
                    recommendation=recommendation,
                    source="analyzer:observability",
                ))

        return DimensionScore(
            dimension="observability",
            score=calculate_score(findings),
            max_score=100,
            findings=findings,
        )
