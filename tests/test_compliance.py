from agentit.analyzers.compliance import ComplianceAnalyzer


def test_no_compliance_scores_zero(create_mock_repo):
    repo = create_mock_repo({"app.py": "print('hi')\n"})
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "compliance"
    assert score.score <= 35


def test_static_sbom_file_does_not_clear_sbom_finding(create_mock_repo):
    repo = create_mock_repo({
        "LICENSE": "Apache License 2.0\n",
        "sbom.json": '{"bomFormat": "CycloneDX", "components": [{"name": "x"}]}',
    })
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    cats = {f.category for f in score.findings}
    assert "sbom" in cats


def test_gha_sbom_action_clears_sbom_finding(create_mock_repo):
    repo = create_mock_repo({
        "LICENSE": "Apache License 2.0\n",
        ".github/workflows/security.yml": (
            "jobs:\n  scan:\n    steps:\n"
            "      - uses: anchore/sbom-action@v0.24.0\n"
        ),
    })
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    cats = {f.category for f in score.findings}
    assert "sbom" not in cats


def test_bare_sbom_task_does_not_clear(create_mock_repo):
    repo = create_mock_repo({
        "LICENSE": "Apache License 2.0\n",
        "sbom-task.yaml": (
            "apiVersion: tekton.dev/v1\nkind: Task\n"
            "metadata:\n  name: app-sbom\n"
            "spec:\n  steps:\n"
            "  - name: generate-sbom\n"
            "    image: anchore/syft:v1.48.0\n"
            "    args: [img, --output, cyclonedx-json=/ws/sbom.json]\n"
        ),
    })
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    assert any(f.category == "sbom" for f in score.findings)


def test_tekton_pipeline_sbom_wire_clears(create_mock_repo):
    repo = create_mock_repo({
        "LICENSE": "Apache License 2.0\n",
        "pipeline.yaml": (
            "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
            "metadata:\n  name: app-pipeline\n"
            "spec:\n  tasks:\n"
            "    - name: sbom-generate\n"
            "      taskRef:\n        name: app-sbom\n"
        ),
    })
    analyzer = ComplianceAnalyzer()
    score = analyzer.analyze(repo)
    assert not any(f.category == "sbom" for f in score.findings)
