from agentit.analyzers.data_governance import DataGovernanceAnalyzer


def test_no_data_governance_scores_low(create_mock_repo):
    repo = create_mock_repo({"app.py": "print('hi')\n"})
    analyzer = DataGovernanceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "data_governance"
    assert score.score <= 45


def test_backup_config_scores_higher(create_mock_repo):
    repo = create_mock_repo({
        "deploy/backup-cronjob.yaml": "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: db-backup\n",
        "deploy/pvc.yaml": "apiVersion: v1\nkind: PersistentVolumeClaim\n",
    })
    analyzer = DataGovernanceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 30
