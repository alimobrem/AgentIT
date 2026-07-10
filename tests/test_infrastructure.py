from agentit.analyzers.infrastructure import InfrastructureAnalyzer


def test_no_infra_scores_low(create_mock_repo):
    repo = create_mock_repo({"app.py": "print('hi')\n"})
    analyzer = InfrastructureAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "infrastructure"
    assert score.score <= 65


def test_helm_chart_scores_medium(create_mock_repo):
    repo = create_mock_repo({
        "chart/Chart.yaml": "apiVersion: v2\nname: myapp\nversion: 1.0.0\n",
        "chart/values.yaml": "replicaCount: 1\n",
        "chart/templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n",
    })
    analyzer = InfrastructureAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 40
