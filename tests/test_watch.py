import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agentit.cli import main


def _make_local_repo(tmp_path: Path) -> str:
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    (repo_dir / "main.go").write_text("package main\nfunc main() {}\n")
    (repo_dir / "go.mod").write_text("module github.com/test/app\n\ngo 1.22\n")
    (repo_dir / "Dockerfile").write_text("FROM golang:1.22\nCMD ['app']\n")
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "T"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
    return str(repo_dir)


def test_watch_runs_one_iteration(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()

    with patch("time.sleep", side_effect=KeyboardInterrupt):
        result = runner.invoke(main, ["watch", repo_url, "--interval", "10"])

    assert result.exit_code == 0, result.output
    assert "Watching" in result.output
    assert "/100" in result.output
    assert "Stopped." in result.output


def test_watch_requires_repo_url_unless_rescan():
    """Regression: repo_url must still be required for normal (non-rescan)
    invocations, even though it's no longer a hard Click-required argument."""
    runner = CliRunner()
    result = runner.invoke(main, ["watch"])
    assert result.exit_code != 0
    assert "REPO_URL is required" in result.output


def test_assess_requires_repo_url_unless_rescan():
    runner = CliRunner()
    result = runner.invoke(main, ["assess"])
    assert result.exit_code != 0
    assert "REPO_URL is required" in result.output


def test_watch_rescan_iterates_the_fleet(tmp_path: Path, monkeypatch):
    """Regression: `agentit watch --rescan` (used by CronJobs, see
    chart/templates/workflows/cve-scan-cronworkflow.yaml) previously failed
    with a Click usage error because --rescan wasn't a real flag. It must
    now iterate every tracked fleet app via the store and re-assess each."""
    from agentit.portal.store import AssessmentStore
    from conftest import make_report

    db_path = str(tmp_path / "fleet.db")
    monkeypatch.setenv("AGENTIT_DB_PATH", db_path)

    repo_url = _make_local_repo(tmp_path)
    store = AssessmentStore(db_path=db_path)
    store.save(make_report(repo_name="tracked-app", repo_url=repo_url, criticality="low"))

    runner = CliRunner()
    result = runner.invoke(main, ["watch", "--rescan"])

    assert result.exit_code == 0, result.output
    assert "[rescan]" in result.output
    assert "Re-assessing 1 fleet app" in result.output

    # A fresh assessment must have been persisted for the tracked app.
    history = store.list_history(repo_url)
    assert len(history) == 2  # the seeded one + the rescan


def test_assess_rescan_with_no_fleet_apps_is_a_noop(tmp_path: Path, monkeypatch):
    db_path = str(tmp_path / "empty-fleet.db")
    monkeypatch.setenv("AGENTIT_DB_PATH", db_path)

    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--rescan"])

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output.lower()


def test_assess_rescan_filters_dimension_count(tmp_path: Path, monkeypatch):
    from agentit.portal.store import AssessmentStore
    from conftest import make_report

    db_path = str(tmp_path / "fleet2.db")
    monkeypatch.setenv("AGENTIT_DB_PATH", db_path)

    repo_url = _make_local_repo(tmp_path)
    store = AssessmentStore(db_path=db_path)
    store.save(make_report(repo_name="tracked-app2", repo_url=repo_url, criticality="low"))

    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--rescan", "--dimension", "compliance"])

    assert result.exit_code == 0, result.output
    assert "dimension=compliance" in result.output
