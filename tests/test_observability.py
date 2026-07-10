from agentit.analyzers.observability import ObservabilityAnalyzer


def test_no_observability_scores_low(create_mock_repo):
    repo = create_mock_repo({"main.go": "package main\nfunc main() {}\n"})
    analyzer = ObservabilityAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "observability"
    assert score.score <= 20


def test_full_observability_scores_high(create_mock_repo):
    repo = create_mock_repo({
        "main.go": 'import "go.opentelemetry.io/otel"\n',
        "deploy/servicemonitor.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\n",
        "deploy/grafana-dashboard.json": '{"dashboard": {}}',
        "deploy/alerting-rules.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n",
    })
    analyzer = ObservabilityAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 60
