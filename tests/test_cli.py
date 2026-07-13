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
    """Extract JSON object from CLI output that may contain stderr lines.

    ``agentit assess --format json`` delimits its payload with
    ``AGENTIT_RESULT_BEGIN``/``END`` markers (see cli.py) so that warning/info
    log lines merged onto the same stream (e.g. by CliRunner, or ``2>&1``)
    can't be mistaken for part of the JSON. Fall back to a naive first-``{``
    search for output that predates the marker convention.
    """
    begin, end = "--- AGENTIT_RESULT_BEGIN ---", "--- AGENTIT_RESULT_END ---"
    if begin in output and end in output:
        return output.split(begin, 1)[1].split(end, 1)[0].strip()
    start = output.index("{")
    return output[start:]


def test_cli_assess_json(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()
    # --no-llm keeps this hermetic: without it, an ambient ANTHROPIC_API_KEY
    # in the environment would trigger a real LLM call, and any failure log
    # from that call could land in result.output alongside the JSON payload.
    result = runner.invoke(main, ["assess", repo_url, "--format", "json", "--no-llm"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(_extract_json(result.output))
    assert parsed["repo_url"] == repo_url
    assert len(parsed["scores"]) == 7


def test_cli_assess_terminal(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["assess", repo_url, "--format", "terminal", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "ENTERPRISE READINESS ASSESSMENT" in result.output
    assert "security" in result.output.lower()


def test_cli_assess_output_file(tmp_path: Path):
    repo_url = _make_local_repo(tmp_path)
    output_file = tmp_path / "report.json"
    runner = CliRunner()
    result = runner.invoke(main, ["assess", repo_url, "--format", "json", "--output", str(output_file), "--no-llm"])
    assert result.exit_code == 0, result.output
    assert output_file.exists()
    parsed = json.loads(output_file.read_text())
    assert parsed["repo_url"] == repo_url
