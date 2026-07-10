from agentit.analyzers.security import SecurityAnalyzer
from agentit.models import Severity


def test_detects_hardcoded_secrets(create_mock_repo):
    repo = create_mock_repo({
        "config.yaml": "database:\n  password: mysecretpassword123\n  host: localhost\n",
        "app.py": 'DB_PASSWORD = "hunter2"\nAPI_KEY = "sk-1234567890abcdef"\n',
    })
    analyzer = SecurityAnalyzer()
    score = analyzer.analyze(repo)
    assert score.dimension == "security"
    secret_findings = [f for f in score.findings if f.category == "secrets"]
    assert len(secret_findings) >= 2
    assert any(f.severity == Severity.critical for f in secret_findings)


def test_detects_dockerfile_running_as_root(create_mock_repo):
    repo = create_mock_repo({
        "Dockerfile": "FROM ubuntu:latest\nRUN apt-get update\nCMD ['app']\n",
    })
    analyzer = SecurityAnalyzer()
    score = analyzer.analyze(repo)
    root_findings = [f for f in score.findings if f.category == "container"]
    assert any("root" in f.description.lower() or "user" in f.description.lower() for f in root_findings)


def test_detects_missing_network_policies(create_mock_repo):
    repo = create_mock_repo({
        "deploy/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n",
    })
    analyzer = SecurityAnalyzer()
    score = analyzer.analyze(repo)
    net_findings = [f for f in score.findings if f.category == "network"]
    assert len(net_findings) >= 1


def test_secure_repo_scores_high(create_mock_repo):
    repo = create_mock_repo({
        "Dockerfile": "FROM registry.access.redhat.com/ubi9/ubi-minimal:latest\nUSER 1001\nHEALTHCHECK CMD curl -f http://localhost:8080/health\nCMD ['app']\n",
        "deploy/networkpolicy.yaml": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n",
        "deploy/rbac.yaml": "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n",
        ".github/workflows/ci.yml": "- uses: aquasecurity/trivy-action@master\n",
    })
    analyzer = SecurityAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score >= 50


def test_empty_repo_scores_low(create_mock_repo):
    repo = create_mock_repo({"README.md": "# App"})
    analyzer = SecurityAnalyzer()
    score = analyzer.analyze(repo)
    assert score.score <= 80
