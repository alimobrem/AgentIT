from agentit.analyzers.cicd import CICDAnalyzer


def test_no_cicd_scores_low(create_mock_repo):
    repo = create_mock_repo({"main.py": "print('hi')\n"})
    analyzer = CICDAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "cicd"
    assert score.score <= 55


def test_github_actions_scores_medium(create_mock_repo):
    repo = create_mock_repo({
        ".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        "Dockerfile": "FROM python:3.12\n",
    })
    analyzer = CICDAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 30


def test_tekton_pipeline_scores_high(create_mock_repo):
    repo = create_mock_repo({
        ".tekton/pipeline.yaml": "apiVersion: tekton.dev/v1\nkind: Pipeline\n",
        "Dockerfile": "FROM python:3.12\nUSER 1001\n",
        "argocd/application.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: Application\n",
    })
    analyzer = CICDAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 60
