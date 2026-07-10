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


def test_onboard_creates_output(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    output_dir = tmp_path / "onboard-out"
    runner = CliRunner()
    result = runner.invoke(main, ["onboard", repo_url, "--output-dir", str(output_dir)])
    assert result.exit_code == 0, result.output

    assert output_dir.exists()
    assert (output_dir / "assessment.json").exists()
    for subdir in ("security", "observability", "cicd", "compliance"):
        assert (output_dir / subdir).is_dir(), f"Missing subdirectory: {subdir}"
