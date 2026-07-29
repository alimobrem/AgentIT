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


def test_replicas_mentioned_in_a_comment_does_not_clear_the_finding(create_mock_repo):
    """Confirmed live gap, previously unfixed: a bare regex match against
    raw YAML file text matched "replicas:" anywhere at all -- including
    inside a YAML comment, which a real parser strips before the analyzer
    ever sees it. A single-replica Deployment must still be flagged even
    when an unrelated file happens to mention a higher number in prose."""
    repo = create_mock_repo({
        "deploy/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: 1\n"
        ),
        "deploy/notes.yaml": (
            "# TODO: bump replicas: 5 once we have capacity\n"
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes\n"
        ),
    })
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert any(f.category == "replicas" for f in score.findings)


def test_replicas_field_on_a_non_workload_resource_does_not_clear_the_finding(create_mock_repo):
    """Same class of bug: a field named "replicas" on some unrelated
    resource (not a Deployment/StatefulSet/Rollout/DaemonSet) must never
    be read as if it were a real workload's replica count."""
    repo = create_mock_repo({
        "deploy/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: 1\n"
        ),
        "deploy/weird.yaml": (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
            "data:\n  replicas: 5\n"
        ),
    })
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert any(f.category == "replicas" for f in score.findings)


def test_disconnected_deployment_stub_still_clears_repo_wide_by_design(create_mock_repo):
    """Documents the remaining, honest limitation: this analyzer has no
    live-cluster context (repo path only), so it cannot tell a real,
    deployed Deployment manifest apart from a fully-formed but genuinely
    orphaned one sitting unused in the repo -- that distinction needs the
    same live workload-discovery access fleet_hpa.py's HPA gate already
    has. This is a known, documented gap, not a regression -- see the
    _workload_replica_counts() docstring."""
    repo = create_mock_repo({
        "deploy/deployment.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: 2\n"
        ),
    })
    analyzer = HADRAnalyzer()
    score = analyzer.analyze(repo)
    assert not any(f.category == "replicas" for f in score.findings)
