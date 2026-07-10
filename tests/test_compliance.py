from agentit.analyzers.compliance import ComplianceAnalyzer


def test_no_compliance_scores_zero(create_mock_repo):
    repo = create_mock_repo({"app.py": "print('hi')\n"})
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "compliance"
    assert score.score <= 35


def test_sbom_and_license_score_medium(create_mock_repo):
    repo = create_mock_repo({
        "LICENSE": "Apache License 2.0\n",
        "sbom.json": '{"bomFormat": "CycloneDX"}',
    })
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 25
