import json
import subprocess
from pathlib import Path

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


def _extract_json(output: str) -> str:
    """Extract JSON object from CLI output that may contain stderr lines."""
    start = output.index("{")
    return output[start:]


def test_cli_assess_json(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["assess", repo_url, "--format", "json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(_extract_json(result.output))
    assert parsed["repo_url"] == repo_url
    assert len(parsed["scores"]) == 7


def test_cli_assess_terminal(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["assess", repo_url, "--format", "terminal"])
    assert result.exit_code == 0, result.output
    assert "ENTERPRISE READINESS ASSESSMENT" in result.output
    assert "security" in result.output.lower()


def test_cli_assess_output_file(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    output_file = tmp_path / "report.json"
    runner = CliRunner()
    result = runner.invoke(main, ["assess", repo_url, "--format", "json", "--output", str(output_file)])
    assert result.exit_code == 0, result.output
    assert output_file.exists()
    parsed = json.loads(output_file.read_text())
    assert parsed["repo_url"] == repo_url
