from agentit.analyzers.ha_dr import HADRAnalyzer


def test_no_ha_scores_low(create_mock_repo):
    repo = create_mock_repo({"app.py": "print('hi')\n"})
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "ha_dr"
    assert score.score <= 45


def test_replicas_and_pdb_score_higher(create_mock_repo):
    repo = create_mock_repo({
        "deploy/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: 3\n",
        "deploy/pdb.yaml": "apiVersion: policy/v1\nkind: PodDisruptionBudget\n",
        "deploy/hpa.yaml": "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n",
    })
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 40
