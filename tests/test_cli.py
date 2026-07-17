import json
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


def test_self_fix_llm_construction_failure_does_not_auto_approve(tmp_path: Path):
    """Regression test for the "first approver gate" fail-open bug.

    When ``LLMClient()`` itself fails to construct (no API key, etc.),
    ``self-fix`` must route every generated fix through the same
    fail-closed "rejected" path used when a successfully-constructed
    client's ``review_fix()`` call returns ``None`` -- not auto-approve
    them. Before the fix, every fix was appended straight to
    ``approved_files`` here, so without ``--dry-run`` every one would be
    written to disk (and, with ``--create-pr``, pushed/opened as a PR)
    despite the command's own "all fixes gated" message and zero actual
    review having taken place.
    """
    repo_url = _make_local_repo(tmp_path)
    runner = CliRunner()

    # discover_platform() would otherwise try to reach a real cluster via
    # local kubeconfig -- force the same offline fallback path used by
    # test_orchestrator.py so this stays hermetic and fast.
    with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no API key configured")), \
         patch("agentit.platform_context.discover_platform", side_effect=RuntimeError("no cluster in tests")), \
         runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        # Deliberately omit --dry-run: this is exactly the unsafe path the
        # bug allowed (fixes written to disk with zero review).
        result = runner.invoke(main, ["self-fix", "--repo-url", repo_url])

        assert result.exit_code == 0, result.output
        assert "auto-approved" not in result.output
        assert "rejected (fail-closed)" in result.output
        assert "Approved: 0" in result.output
        assert "No fixes approved by LLM." in result.output

        # Nothing should have been written to disk -- confirms the gate
        # actually blocked application, not just the summary line.
        written = list(Path(cwd).rglob("*.yaml"))
        assert written == [], f"fix(es) written to disk despite failed review: {written}"


def _make_functional_draft_skill(path: Path, name: str = "cli-activate-test", domain: str = "security") -> None:
    """A draft skill that actually generates valid output via its own
    template body -- mirrors ``tests/test_portal.py``'s ``_make_draft_skill``
    so ``verify_skill()``'s functional generation smoke test passes."""
    path.write_text(
        f"---\n"
        f"name: {name}\n"
        f"domain: {domain}\n"
        f"version: 1\n"
        f"triggers: [test]\n"
        f"outputs: [NetworkPolicy]\n"
        f"status: draft\n"
        f"---\n"
        "## Property\nEnsures network isolation.\n\n"
        "## Constraints\nMust apply to all pods.\n\n"
        "## Verification\nCheck that a NetworkPolicy restricting Ingress exists.\n\n"
        "```yaml\n"
        "apiVersion: networking.k8s.io/v1\n"
        "kind: NetworkPolicy\n"
        "metadata:\n"
        "  name: {{app_name}}-netpol\n"
        "spec:\n"
        "  podSelector: {}\n"
        "  policyTypes:\n"
        "    - Ingress\n"
        "```\n",
        encoding="utf-8",
    )


def test_activate_skill_blocks_when_verification_fails(tmp_path: Path):
    """Regression test: ``activate-skill`` previously only checked for the
    literal "status: draft" string and string-replaced it to "active" --
    no ``load_skill()``/``verify_skill()`` functional check at all, unlike
    the portal's documented-equivalent action
    (``routes/capabilities.py::activate_skill_route``). A draft skill with
    no usable template must now be blocked, not silently promoted.
    """
    skill_file = tmp_path / "nonfunctional.md"
    skill_file.write_text(
        "---\nname: nonfunctional\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\nstatus: draft\n---\nno template here\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["activate-skill", str(skill_file)])

    assert result.exit_code != 0
    assert "Activation blocked" in result.output
    assert "status: draft" in skill_file.read_text()


def test_activate_skill_promotes_valid_draft(tmp_path: Path):
    """A genuinely valid draft skill (real triggers/outputs and a usable
    template body) still activates successfully through the new
    ``verify_skill()`` gate."""
    skill_file = tmp_path / "cli-activate-test.md"
    _make_functional_draft_skill(skill_file)

    runner = CliRunner()
    result = runner.invoke(main, ["activate-skill", str(skill_file)])

    assert result.exit_code == 0, result.output
    assert "Activated" in result.output
    assert "status: active" in skill_file.read_text()
