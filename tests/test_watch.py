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
