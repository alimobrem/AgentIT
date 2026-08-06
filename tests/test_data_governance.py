from agentit.analyzers.data_governance import DataGovernanceAnalyzer


def test_no_data_governance_scores_low(create_mock_repo):
    # Needs a real database dependency, or the "migration" finding is
    # correctly suppressed (has_database_dependency precondition) --
    # this fixture is meant to test "has data, no governance," not
    # "no data at all."
    repo = create_mock_repo({
        "app.py": "print('hi')\n",
        "requirements.txt": "psycopg2-binary\n",
    })
    analyzer = DataGovernanceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "data_governance"
    assert score.score <= 65


def test_backup_config_scores_higher(create_mock_repo):
    repo = create_mock_repo({
        "deploy/backup-cronjob.yaml": "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: db-backup\n",
        "deploy/pvc.yaml": "apiVersion: v1\nkind: PersistentVolumeClaim\n",
    })
    analyzer = DataGovernanceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 30
