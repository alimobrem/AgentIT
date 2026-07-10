from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.portal.github_pr import create_onboarding_pr


SAMPLE_FILES = [
    {
        "category": "security",
        "path": "networkpolicy.yaml",
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy",
        "description": "Default deny ingress NetworkPolicy",
    },
    {
        "category": "observability",
        "path": "servicemonitor.yaml",
        "content": "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor",
        "description": "Prometheus ServiceMonitor",
    },
]


@patch("agentit.portal.github_pr.shutil.rmtree")
@patch("agentit.portal.github_pr.subprocess.run")
@patch("agentit.portal.github_pr.tempfile.mkdtemp")
def test_create_onboarding_pr_structure(mock_mkdtemp, mock_run, mock_rmtree, tmp_path):
    """Verify the function issues git clone, checkout, add, commit, push, and gh pr create in order."""
    work_dir = str(tmp_path / "agentit-pr-xyz")
    mock_mkdtemp.return_value = work_dir

    # Make the repo dir exist so Path writes succeed
    repo_dir = tmp_path / "agentit-pr-xyz" / "my-app"
    repo_dir.mkdir(parents=True)

    # gh pr create returns the PR URL on stdout
    def side_effect(*args, **kwargs):
        cmd = args[0]
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        if cmd[0] == "gh":
            result.stdout = "https://github.com/org/my-app/pull/42\n"
        return result

    mock_run.side_effect = side_effect

    result = create_onboarding_pr(
        repo_url="https://github.com/org/my-app.git",
        repo_name="my-app",
        files=SAMPLE_FILES,
        branch_name="agentit/onboarding",
    )

    assert result["pr_url"] == "https://github.com/org/my-app/pull/42"
    assert result["branch"] == "agentit/onboarding"
    assert result["files_added"] == 2

    # Verify the sequence of subprocess calls
    calls = mock_run.call_args_list
    assert len(calls) == 6

    # 1. git clone
    assert calls[0].args[0][:2] == ["git", "clone"]

    # 2. git checkout -b
    assert calls[1].args[0] == ["git", "checkout", "-b", "agentit/onboarding"]

    # 3. git add
    assert calls[2].args[0] == ["git", "add", ".agentit"]

    # 4. git commit
    assert calls[3].args[0][0:2] == ["git", "commit"]

    # 5. git push
    assert calls[4].args[0] == ["git", "push", "-u", "origin", "agentit/onboarding"]

    # 6. gh pr create
    assert calls[5].args[0][0:3] == ["gh", "pr", "create"]

    # Verify files were written to disk
    sec = repo_dir / ".agentit" / "security" / "networkpolicy.yaml"
    obs = repo_dir / ".agentit" / "observability" / "servicemonitor.yaml"
    assert sec.exists()
    assert obs.exists()

    # Verify cleanup
    mock_rmtree.assert_called_once_with(work_dir, ignore_errors=True)


@patch("agentit.portal.github_pr.shutil.rmtree")
@patch("agentit.portal.github_pr.subprocess.run")
@patch("agentit.portal.github_pr.tempfile.mkdtemp")
def test_create_onboarding_pr_error(mock_mkdtemp, mock_run, mock_rmtree, tmp_path):
    """Verify subprocess failures return an error dict instead of raising."""
    import subprocess

    work_dir = str(tmp_path / "agentit-pr-fail")
    mock_mkdtemp.return_value = work_dir
    (tmp_path / "agentit-pr-fail").mkdir(parents=True)

    mock_run.side_effect = subprocess.CalledProcessError(
        128, ["git", "clone"], stderr="fatal: repo not found",
    )

    result = create_onboarding_pr(
        repo_url="https://github.com/org/nope.git",
        repo_name="nope",
        files=SAMPLE_FILES,
    )

    assert "error" in result
    assert "git" in result["error"]
    mock_rmtree.assert_called_once()
