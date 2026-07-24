"""Tests for bring-your-own-GitOps-repo onboarding support.

Covers the three ways a human-supplied GitOps infra repo can resolve
(``github_pr.ensure_custom_gitops_repo()``): exists + AgentIT has push
access (reused as-is), doesn't exist (created empty, org-aware), exists
but AgentIT lacks push access (hard refusal, never a silent substitute
repo); how that plugs into the mandatory Assess-time gate
(``assess_pipeline._resolve_mandatory_infra_repo_url()``); the real-data
default-repo-URL placeholder helper (``github_pr.default_infra_repo_url()``);
and the empty-repo bootstrap that lets ``commit_to_infra_repo()``'s existing
commit/PR machinery work against a repo AgentIT just created with zero
commits (``_get_default_branch_and_base_sha()``).

All GitHub API calls are mocked (``agentit.portal.github_pr.requests``) --
no live network calls, no live GitHub/cluster access, per this repo's
no-mock-data-except-tests convention.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from agentit.portal import github_pr
from agentit.portal.github_pr import (
    commit_to_infra_repo,
    default_infra_repo_url,
    ensure_custom_gitops_repo,
)
from agentit.portal.services import assess_pipeline
from agentit.portal.services.assess_pipeline import (
    InfraRepoRequiredError,
    _resolve_mandatory_infra_repo_url,
)


@pytest.fixture(autouse=True)
def _reset_default_infra_repo_cache():
    """``default_infra_repo_url()`` caches in a module-level dict --
    reset it before/after every test so one test's mocked token identity
    can never leak into another's assertions."""
    github_pr._default_infra_repo_url_cache["value"] = None
    github_pr._default_infra_repo_url_cache["ts"] = 0.0
    yield
    github_pr._default_infra_repo_url_cache["value"] = None
    github_pr._default_infra_repo_url_cache["ts"] = 0.0


# ── ensure_custom_gitops_repo: exists + accessible ──────────────────────


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_existing_repo_with_push_access_is_reused_not_recreated(mock_requests):
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {
        "html_url": "https://github.com/customorg/custom-gitops",
        "permissions": {"push": True, "admin": False, "pull": True},
    }
    mock_requests.get.return_value = get_resp

    result = ensure_custom_gitops_repo("https://github.com/customorg/custom-gitops")

    assert result == {
        "repo_url": "https://github.com/customorg/custom-gitops",
        "created": False,
    }
    mock_requests.post.assert_not_called()


# ── ensure_custom_gitops_repo: exists but no access -> hard refusal ─────


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_existing_repo_without_push_access_is_refused_not_substituted(mock_requests):
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {
        "html_url": "https://github.com/customorg/custom-gitops",
        "permissions": {"push": False, "admin": False, "pull": True},
    }
    mock_requests.get.return_value = get_resp

    result = ensure_custom_gitops_repo("https://github.com/customorg/custom-gitops")

    assert "error" in result
    assert "push" in result["error"].lower() or "access" in result["error"].lower()
    # Never silently create/reuse a different repo the user didn't ask for.
    mock_requests.post.assert_not_called()


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_existing_repo_with_no_permissions_field_at_all_is_refused(mock_requests):
    """A response with no `permissions` key at all (e.g. some unauthenticated
    or restricted-scope views) must be treated as "no access", not crash."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {"html_url": "https://github.com/customorg/custom-gitops"}
    mock_requests.get.return_value = get_resp

    result = ensure_custom_gitops_repo("https://github.com/customorg/custom-gitops")

    assert "error" in result
    mock_requests.post.assert_not_called()


# ── ensure_custom_gitops_repo: doesn't exist -> created, org-aware ──────


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_missing_repo_created_via_orgs_endpoint_for_org_owner(mock_requests):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/customorg/new-gitops"):
            resp.status_code = 404
        elif url.endswith("/users/customorg"):
            resp.status_code = 200
            resp.json.return_value = {"type": "Organization"}
        return resp

    mock_requests.get.side_effect = mock_get

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json.return_value = {"html_url": "https://github.com/customorg/new-gitops"}
    mock_requests.post.return_value = post_resp

    result = ensure_custom_gitops_repo("https://github.com/customorg/new-gitops")

    assert result == {"repo_url": "https://github.com/customorg/new-gitops", "created": True}
    create_call = mock_requests.post.call_args
    assert "/orgs/customorg/repos" in create_call.args[0]
    assert create_call.kwargs["json"]["private"] is True
    # Empty repo, per the product decision -- no README/starter scaffold.
    assert create_call.kwargs["json"]["auto_init"] is False


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_missing_repo_created_via_user_repos_when_owner_matches_token_login(mock_requests):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/myaccount/new-gitops"):
            resp.status_code = 404
        elif url.endswith("/users/myaccount"):
            resp.status_code = 200
            resp.json.return_value = {"type": "User"}
        elif url.endswith("/user"):
            resp.status_code = 200
            resp.ok = True
            resp.json.return_value = {"login": "myaccount"}
        return resp

    mock_requests.get.side_effect = mock_get

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json.return_value = {"html_url": "https://github.com/myaccount/new-gitops"}
    mock_requests.post.return_value = post_resp

    result = ensure_custom_gitops_repo("https://github.com/myaccount/new-gitops")

    assert result == {"repo_url": "https://github.com/myaccount/new-gitops", "created": True}
    create_call = mock_requests.post.call_args
    assert create_call.args[0].endswith("/user/repos")


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_missing_repo_under_a_different_users_account_is_refused_not_redirected(mock_requests):
    """GitHub has no API to create a repo directly under someone else's
    personal account -- must refuse with an explicit message, never
    silently create it somewhere else (e.g. the token's own account)."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/someoneelse/new-gitops"):
            resp.status_code = 404
        elif url.endswith("/users/someoneelse"):
            resp.status_code = 200
            resp.json.return_value = {"type": "User"}
        elif url.endswith("/user"):
            resp.status_code = 200
            resp.ok = True
            resp.json.return_value = {"login": "agentit-bot"}
        return resp

    mock_requests.get.side_effect = mock_get

    result = ensure_custom_gitops_repo("https://github.com/someoneelse/new-gitops")

    assert "error" in result
    assert "someoneelse" in result["error"]
    mock_requests.post.assert_not_called()


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_owner_type_lookup_failure_is_refused_not_a_guess(mock_requests):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/customorg/new-gitops"):
            resp.status_code = 404
        elif url.endswith("/users/customorg"):
            resp.status_code = 404  # lookup itself failed
        return resp

    mock_requests.get.side_effect = mock_get

    result = ensure_custom_gitops_repo("https://github.com/customorg/new-gitops")

    assert "error" in result
    mock_requests.post.assert_not_called()


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_creation_api_failure_surfaces_actionable_error(mock_requests):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/customorg/new-gitops"):
            resp.status_code = 404
        elif url.endswith("/users/customorg"):
            resp.status_code = 200
            resp.json.return_value = {"type": "Organization"}
        return resp

    mock_requests.get.side_effect = mock_get

    post_resp = MagicMock()
    post_resp.status_code = 403
    post_resp.text = "Resource not accessible by integration"
    mock_requests.post.return_value = post_resp

    result = ensure_custom_gitops_repo("https://github.com/customorg/new-gitops")

    assert "error" in result
    assert "403" in result["error"]


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_existence_check_unexpected_status_is_refused(mock_requests):
    """Neither 200 (exists) nor 404 (doesn't exist) -- e.g. a transient 500
    -- must not be silently treated as either case."""
    get_resp = MagicMock()
    get_resp.status_code = 500
    get_resp.text = "Internal Server Error"
    mock_requests.get.return_value = get_resp

    result = ensure_custom_gitops_repo("https://github.com/customorg/new-gitops")

    assert "error" in result
    mock_requests.post.assert_not_called()


# ── _resolve_mandatory_infra_repo_url: wired into the real Assess gate ──


def test_human_supplied_existing_accessible_repo_used_as_is():
    # `_resolve_mandatory_infra_repo_url()` does a local
    # `from agentit.portal.github_pr import ...` at call time, so the patch
    # target is the `github_pr` module itself, not `assess_pipeline`'s
    # (nonexistent) attribute of the same name.
    with patch(
        "agentit.portal.github_pr.ensure_custom_gitops_repo",
        return_value={"repo_url": "https://github.com/customorg/custom-gitops", "created": False},
    ) as mock_ensure:
        result = _resolve_mandatory_infra_repo_url(
            "https://github.com/customorg/my-app",
            "https://github.com/customorg/custom-gitops",
        )

    assert result == "https://github.com/customorg/custom-gitops"
    mock_ensure.assert_called_once_with("https://github.com/customorg/custom-gitops")


def test_human_supplied_missing_repo_gets_created_and_used():
    with patch(
        "agentit.portal.github_pr.ensure_custom_gitops_repo",
        return_value={"repo_url": "https://github.com/customorg/brand-new-gitops", "created": True},
    ):
        result = _resolve_mandatory_infra_repo_url(
            "https://github.com/customorg/my-app",
            "https://github.com/customorg/brand-new-gitops",
        )

    assert result == "https://github.com/customorg/brand-new-gitops"


def test_human_supplied_repo_error_raises_infra_repo_required_error_not_fabricated_success():
    with patch(
        "agentit.portal.github_pr.ensure_custom_gitops_repo",
        return_value={"error": "GitOps repo 'customorg/custom-gitops' already exists, but AgentIT's GitHub token does not have write (push) access to it"},
    ):
        with pytest.raises(InfraRepoRequiredError) as excinfo:
            _resolve_mandatory_infra_repo_url(
                "https://github.com/customorg/my-app",
                "https://github.com/customorg/custom-gitops",
            )

    assert "push" in str(excinfo.value).lower() or "access" in str(excinfo.value).lower()


def test_human_supplied_untrusted_host_rejected_before_any_github_call():
    """The cheap trusted-host check must still short-circuit before ever
    reaching ensure_custom_gitops_repo() -- no wasted GitHub API call for a
    URL that could never be usable anyway."""
    with patch("agentit.portal.github_pr.ensure_custom_gitops_repo") as mock_ensure:
        with pytest.raises(InfraRepoRequiredError, match="trusted Git host"):
            _resolve_mandatory_infra_repo_url(
                "https://github.com/customorg/my-app",
                "https://evil.example.com/customorg/custom-gitops",
            )

    mock_ensure.assert_not_called()


def test_blank_default_path_behavior_is_unchanged_regression():
    """No repo supplied must still go through `_auto_create_infra_repo()`
    exactly as before -- the confirmed-unchanged default path -- and must
    never touch the new custom-repo existence/access/create logic at all."""
    with patch.object(
        assess_pipeline, "_auto_create_infra_repo",
        return_value="https://github.com/alimobrem/agentit-gitops",
    ) as mock_auto_create, patch("agentit.portal.github_pr.ensure_custom_gitops_repo") as mock_ensure:
        result = _resolve_mandatory_infra_repo_url("https://github.com/customorg/my-app", None)

    assert result == "https://github.com/alimobrem/agentit-gitops"
    mock_auto_create.assert_called_once_with("https://github.com/customorg/my-app")
    mock_ensure.assert_not_called()


def test_blank_default_path_still_raises_when_auto_create_fails_regression():
    with patch.object(assess_pipeline, "_auto_create_infra_repo", return_value=None):
        with pytest.raises(InfraRepoRequiredError, match="auto-create"):
            _resolve_mandatory_infra_repo_url("https://github.com/customorg/my-app", None)


# ── default_infra_repo_url(): real value for the Assess form's hint ────


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_default_infra_repo_url_resolves_from_authenticated_token_login(mock_requests):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"login": "alimobrem"}
    mock_requests.get.return_value = resp

    assert default_infra_repo_url() == "https://github.com/alimobrem/agentit-gitops"


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_default_infra_repo_url_is_cached_not_refetched_every_call(mock_requests):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"login": "alimobrem"}
    mock_requests.get.return_value = resp

    first = default_infra_repo_url()
    second = default_infra_repo_url()

    assert first == second == "https://github.com/alimobrem/agentit-gitops"
    assert mock_requests.get.call_count == 1


@patch.dict(os.environ, {}, clear=True)
def test_default_infra_repo_url_returns_none_gracefully_when_token_missing():
    assert default_infra_repo_url() is None


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_default_infra_repo_url_returns_none_gracefully_on_api_failure(mock_requests):
    resp = MagicMock()
    resp.status_code = 401
    mock_requests.get.return_value = resp

    assert default_infra_repo_url() is None


# ── Empty-repo bootstrap: commit_to_infra_repo() against a brand-new,
# zero-commit custom repo (ensure_custom_gitops_repo() creates it with
# auto_init=False, per the "no scaffold" product decision) ─────────────


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_commit_to_infra_repo_bootstraps_a_brand_new_empty_repo(mock_requests):
    """A repo `ensure_custom_gitops_repo()` just created has zero commits --
    `refs/heads/{default_branch}` 404s. commit_to_infra_repo()'s normal
    tree/commit/ref/PR flow must still succeed by transparently
    bootstrapping that ref first, with no files of its own."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/customorg/new-app-gitops"):
            resp.status_code = 200
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.status_code = 404  # brand new repo, no commits yet
        elif "/contents/" in url:
            resp.status_code = 404  # nothing committed yet either
        return resp

    def mock_post(url, **kwargs):
        # Deliberately no assertions inside this side_effect -- an
        # AssertionError raised here would just be swallowed by
        # commit_to_infra_repo()'s own broad `except Exception` and turned
        # into a confusing {"error": ...} result instead of a clear test
        # failure. All shape assertions happen below, against the real
        # recorded call_args_list, once the call has actually returned.
        resp = MagicMock()
        resp.status_code = 201
        body = kwargs.get("json") or {}
        if "git/commits" in url:
            if body.get("parents") == []:
                resp.json.return_value = {"sha": "bootstrap-commit-sha"}
            else:
                resp.json.return_value = {"sha": "real-commit-sha"}
        elif "git/trees" in url:
            resp.json.return_value = {"sha": "real-tree-sha"}
        elif "git/refs" in url:
            resp.json.return_value = {"ref": body.get("ref", "")}
        elif url.endswith("/pulls"):
            resp.json.return_value = {
                "html_url": "https://github.com/customorg/new-app-gitops/pull/1",
            }
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post

    result = commit_to_infra_repo(
        infra_repo_url="https://github.com/customorg/new-app-gitops",
        app_name="new-app",
        files=[{
            "category": "security", "path": "networkpolicy.yaml",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy",
            "description": "Default deny",
        }],
    )

    assert result["pr_url"] == "https://github.com/customorg/new-app-gitops/pull/1"
    assert result["files_committed"] == 1

    commit_calls = [c for c in mock_requests.post.call_args_list if "git/commits" in str(c)]
    assert len(commit_calls) == 2
    bootstrap_commit = next(c for c in commit_calls if c.kwargs["json"]["parents"] == [])
    assert bootstrap_commit.kwargs["json"]["tree"] == github_pr._EMPTY_TREE_SHA
    real_commit = next(c for c in commit_calls if c.kwargs["json"]["parents"] != [])
    assert real_commit.kwargs["json"]["parents"] == ["bootstrap-commit-sha"]

    tree_call = next(c for c in mock_requests.post.call_args_list if "git/trees" in str(c))
    assert tree_call.kwargs["json"]["base_tree"] == "bootstrap-commit-sha"

    # Two distinct `git/refs` calls: the default-branch bootstrap ref, and
    # the real feature branch ref.
    ref_calls = [c for c in mock_requests.post.call_args_list if "git/refs" in str(c)]
    assert len(ref_calls) == 2
    assert any(c.kwargs["json"]["ref"] == "refs/heads/main" for c in ref_calls)
    assert any(c.kwargs["json"]["ref"] == "refs/heads/agentit/new-app" for c in ref_calls)


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_get_default_branch_and_base_sha_bootstraps_empty_repo_directly(mock_requests):
    """Lower-level, direct unit test of the bootstrap logic itself, isolated
    from commit_to_infra_repo()'s other behavior (dedup check, PR open,
    etc.)."""
    from agentit.portal.github_pr import _get_default_branch_and_base_sha

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url == "https://api.github.com/repos/org/empty-repo":
            resp.status_code = 200
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.status_code = 404
        return resp

    commit_resp = MagicMock()
    commit_resp.status_code = 201
    commit_resp.json.return_value = {"sha": "bootstrap-sha"}
    ref_resp = MagicMock()
    ref_resp.status_code = 201

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = lambda url, **kwargs: (
        commit_resp if "git/commits" in url else ref_resp
    )

    default_branch, base_sha = _get_default_branch_and_base_sha(
        "https://api.github.com/repos/org/empty-repo", {"Authorization": "Bearer x"},
    )

    assert default_branch == "main"
    assert base_sha == "bootstrap-sha"
    commit_call = mock_requests.post.call_args_list[0]
    assert commit_call.kwargs["json"]["parents"] == []
    assert commit_call.kwargs["json"]["tree"] == github_pr._EMPTY_TREE_SHA
    ref_call = mock_requests.post.call_args_list[1]
    assert ref_call.kwargs["json"] == {"ref": "refs/heads/main", "sha": "bootstrap-sha"}


@patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_get_default_branch_and_base_sha_unchanged_for_non_empty_repo(mock_requests):
    """Regression: a normal, non-empty repo must take the exact same path
    as before -- no bootstrap commit, no extra API calls."""
    from agentit.portal.github_pr import _get_default_branch_and_base_sha

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url == "https://api.github.com/repos/org/normal-repo":
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "existing-sha"}}
        return resp

    mock_requests.get.side_effect = mock_get

    default_branch, base_sha = _get_default_branch_and_base_sha(
        "https://api.github.com/repos/org/normal-repo", {"Authorization": "Bearer x"},
    )

    assert (default_branch, base_sha) == ("main", "existing-sha")
    mock_requests.post.assert_not_called()
