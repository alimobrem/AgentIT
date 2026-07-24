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


def test_helm_templated_replicas_resolved_via_values_yaml(create_mock_repo):
    """A Helm chart's ``replicas: {{ .Values.replicaCount }}`` never has the
    literal number in the template — it must resolve against values.yaml's
    own replicaCount, or every Helm app is falsely flagged as single-replica
    forever, even after a human (or AgentIT) sets replicaCount: 2 correctly."""
    repo = create_mock_repo({
        "chart/templates/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
            "  replicas: {{ .Values.replicaCount }}\n"
        ),
        "chart/values.yaml": "replicaCount: 2\nimage:\n  tag: latest\n",
    })
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert not any(f.category == "replicas" for f in score.findings)


def test_helm_templated_replicas_still_flagged_when_values_below_minimum(create_mock_repo):
    repo = create_mock_repo({
        "chart/templates/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
            "  replicas: {{ .Values.replicaCount }}\n"
        ),
        "chart/values.yaml": "replicaCount: 1\n",
    })
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert any(f.category == "replicas" for f in score.findings)
