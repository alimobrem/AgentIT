from unittest.mock import MagicMock

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


# ── secret_decisions_out: classify_secret decision capture ──────────────────
# See llm_decisions.py's module docstring / README's "Auditing LLM decisions"
# section -- these per-match verdicts previously vanished after gating a
# finding; secret_decisions_out is how the caller (runner.run_assessment)
# now captures them for persistence via build_secret_classify_events().


def test_kept_verdict_recorded_in_secret_decisions_out(create_mock_repo):
    repo = create_mock_repo({"app.py": 'DB_PASSWORD = "hunter2hunter2"\n'})
    fake_llm = MagicMock()
    fake_llm.classify_secret.return_value = {
        "is_secret": True, "confidence": 0.92, "reason": "Looks like a real hardcoded password",
    }
    decisions: list[dict] = []
    analyzer = SecurityAnalyzer(llm_client=fake_llm, secret_decisions_out=decisions)
    score = analyzer.analyze(repo)

    assert len(decisions) == 1
    d = decisions[0]
    assert d["file_path"] == "app.py"
    assert d["is_secret"] is True
    assert d["confidence"] == 0.92
    assert d["kept"] is True
    # A kept verdict means the finding survives into the report.
    secret_findings = [f for f in score.findings if f.category == "secrets"]
    assert len(secret_findings) == 1


def test_dropped_verdict_recorded_in_secret_decisions_out(create_mock_repo):
    # Deliberately doesn't match any of `_is_false_positive`'s deterministic
    # placeholder/comment heuristics -- the LLM has to be the one that drops it.
    repo = create_mock_repo({"app.py": 'SECRET_KEY = "d4c8f1e2b3a4c5d6e7f80123"\n'})
    fake_llm = MagicMock()
    fake_llm.classify_secret.return_value = {
        "is_secret": False, "confidence": 0.85, "reason": "Reads from an environment variable",
    }
    decisions: list[dict] = []
    analyzer = SecurityAnalyzer(llm_client=fake_llm, secret_decisions_out=decisions)
    score = analyzer.analyze(repo)

    assert len(decisions) == 1
    d = decisions[0]
    assert d["is_secret"] is False
    assert d["kept"] is False
    # A confidently-dropped verdict means the finding never reaches the report.
    secret_findings = [f for f in score.findings if f.category == "secrets"]
    assert len(secret_findings) == 0


def test_low_confidence_false_positive_is_kept_not_dropped(create_mock_repo):
    """`is_secret=False` alone isn't enough to drop -- confidence must be > 0.7
    (see `_check_secrets`'s fail-conservative gate)."""
    repo = create_mock_repo({"app.py": 'API_KEY = "abcdef0123456789"\n'})
    fake_llm = MagicMock()
    fake_llm.classify_secret.return_value = {
        "is_secret": False, "confidence": 0.5, "reason": "Uncertain",
    }
    decisions: list[dict] = []
    analyzer = SecurityAnalyzer(llm_client=fake_llm, secret_decisions_out=decisions)
    score = analyzer.analyze(repo)

    assert decisions[0]["kept"] is True
    secret_findings = [f for f in score.findings if f.category == "secrets"]
    assert len(secret_findings) == 1


def test_secret_decisions_out_none_by_default_is_safe(create_mock_repo):
    """Callers that don't care about decisions (e.g. existing tests/CLI
    paths) pass no `secret_decisions_out` -- must not raise."""
    repo = create_mock_repo({"app.py": 'DB_PASSWORD = "hunter2hunter2"\n'})
    fake_llm = MagicMock()
    fake_llm.classify_secret.return_value = {
        "is_secret": True, "confidence": 0.9, "reason": "real",
    }
    analyzer = SecurityAnalyzer(llm_client=fake_llm)
    score = analyzer.analyze(repo)
    assert any(f.category == "secrets" for f in score.findings)


def test_no_llm_client_makes_no_secret_decisions(create_mock_repo):
    repo = create_mock_repo({"app.py": 'DB_PASSWORD = "hunter2hunter2"\n'})
    decisions: list[dict] = []
    analyzer = SecurityAnalyzer(llm_client=None, secret_decisions_out=decisions)
    analyzer.analyze(repo)
    assert decisions == []


# ---------------------------------------------------------------------------
# run_assessment wiring (regression guard, mirrors test_eol_analyzer.py's
# TestRunAssessmentForwardsLlmClientToEol -- secret_decisions_out must
# actually reach SecurityAnalyzer, not just be accepted and dropped).
# ---------------------------------------------------------------------------


def test_run_assessment_forwards_secret_decisions_out_to_security_analyzer(create_mock_repo):
    repo = create_mock_repo({"app.py": 'DB_PASSWORD = "hunter2hunter2"\n'})
    fake_llm = MagicMock()
    fake_llm.classify_secret.return_value = {
        "is_secret": True, "confidence": 0.9, "reason": "Looks like a real hardcoded password",
    }
    fake_llm.summarize_architecture.return_value = None

    from agentit.runner import run_assessment
    decisions: list[dict] = []
    run_assessment(
        repo, repo_url="https://github.com/test/app", criticality="medium",
        llm_client=fake_llm, secret_decisions_out=decisions,
    )
    assert len(decisions) == 1
    assert decisions[0]["file_path"] == "app.py"
    assert decisions[0]["kept"] is True


def test_run_assessment_without_secret_decisions_out_is_safe(create_mock_repo):
    repo = create_mock_repo({"app.py": 'DB_PASSWORD = "hunter2hunter2"\n'})
    from agentit.runner import run_assessment
    report = run_assessment(
        repo, repo_url="https://github.com/test/app", criticality="medium", llm_client=None,
    )
    security_score = next(s for s in report.scores if s.dimension == "security")
    assert any(f.category == "secrets" for f in security_score.findings)
