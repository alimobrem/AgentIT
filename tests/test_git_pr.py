"""Tests for git_pr.py -- shared git branch/commit/push + REST draft-PR open
mechanics, extracted from cli.py's `self-fix --create-pr` and reused by
capability_scout.py (docs/self-improvement-for-agentit.md)."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from agentit.git_pr import create_branch_commit_push, open_draft_pr


class TestCreateBranchCommitPush:
    def test_success_runs_checkout_add_commit_push_in_order(self):
        with patch("agentit.git_pr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = create_branch_commit_push(
                "agentit/self-improve/foo-123", ["docs/proposals/foo.md"], "docs: propose foo",
            )

        assert result == {"success": True, "branch": "agentit/self-improve/foo-123"}
        calls = mock_run.call_args_list
        assert len(calls) == 4
        assert calls[0].args[0] == ["git", "checkout", "-b", "agentit/self-improve/foo-123"]
        assert calls[1].args[0] == ["git", "add", "docs/proposals/foo.md"]
        assert calls[2].args[0] == ["git", "commit", "-m", "docs: propose foo"]
        assert calls[3].args[0] == ["git", "push", "-u", "origin", "agentit/self-improve/foo-123"]

    def test_passes_cwd_through_to_every_subprocess_call(self, tmp_path):
        with patch("agentit.git_pr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            create_branch_commit_push("branch", ["f.py"], "msg", cwd=tmp_path)

        for call in mock_run.call_args_list:
            assert call.kwargs["cwd"] == tmp_path

    def test_git_failure_returns_error_without_raising(self):
        with patch("agentit.git_pr.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=["git", "push"], stderr="remote rejected",
            )
            result = create_branch_commit_push("branch", ["f.py"], "msg")

        assert result["success"] is False
        assert "remote rejected" in result["error"]

    def test_missing_git_binary_returns_error_without_raising(self):
        with patch("agentit.git_pr.subprocess.run", side_effect=OSError("git not found")):
            result = create_branch_commit_push("branch", ["f.py"], "msg")

        assert result["success"] is False
        assert "git not found" in result["error"]


class TestOpenDraftPr:
    def test_success_returns_pr_url_via_rest(self):
        with patch(
            "agentit.portal.github_pr.open_draft_pull_request",
            return_value={"pr_url": "https://github.com/org/agentit/pull/42"},
        ) as mock_open, patch(
            "agentit.portal.github_pr.resolve_agentit_repo_url",
            return_value="https://github.com/org/agentit",
        ):
            result = open_draft_pr("branch", "Title", "Body")

        assert result == {"pr_url": "https://github.com/org/agentit/pull/42"}
        mock_open.assert_called_once()
        assert mock_open.call_args.kwargs["head"] == "branch"
        assert mock_open.call_args.kwargs["title"] == "Title"
        assert mock_open.call_args.args[0] == "https://github.com/org/agentit"

    def test_api_failure_returns_error(self):
        with patch(
            "agentit.portal.github_pr.open_draft_pull_request",
            return_value={"error": "authentication failed"},
        ), patch(
            "agentit.portal.github_pr.resolve_agentit_repo_url",
            return_value="https://github.com/org/agentit",
        ):
            result = open_draft_pr("branch", "Title", "Body")

        assert "error" in result
        assert "authentication failed" in result["error"]

    def test_missing_token_returns_error_without_raising(self):
        with patch(
            "agentit.portal.github_pr.open_draft_pull_request",
            return_value={"error": "GITHUB_TOKEN env var not set — cannot create PR"},
        ), patch(
            "agentit.portal.github_pr.resolve_agentit_repo_url",
            return_value="https://github.com/org/agentit",
        ):
            result = open_draft_pr("branch", "Title", "Body")

        assert "error" in result
