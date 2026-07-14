from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.portal.github_pr import create_onboarding_pr, ensure_applicationset, ensure_infra_repo


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


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_create_onboarding_pr_structure(mock_requests):
    """Verify the function calls GitHub API to create tree, commit, ref, and PR."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/repos/org/my-app"):
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "abc123"}}
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        if "git/trees" in url:
            resp.json.return_value = {"sha": "tree456"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "commit789"}
        elif "git/refs" in url:
            resp.json.return_value = {"ref": "refs/heads/agentit/onboarding"}
        elif "/pulls" in url:
            resp.json.return_value = {"html_url": "https://github.com/org/my-app/pull/42"}
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post

    result = create_onboarding_pr(
        repo_url="https://github.com/org/my-app.git",
        repo_name="my-app",
        files=SAMPLE_FILES,
        branch_name="agentit/onboarding",
    )

    assert result["pr_url"] == "https://github.com/org/my-app/pull/42"
    assert result["branch"] == "agentit/onboarding"
    assert result["files_added"] == 2

    assert mock_requests.get.call_count == 2
    assert mock_requests.post.call_count >= 3

    tree_call = [c for c in mock_requests.post.call_args_list if "git/trees" in str(c)]
    assert len(tree_call) == 1
    tree_items = tree_call[0].kwargs["json"]["tree"]
    paths = {t["path"] for t in tree_items}
    assert ".agentit/security/networkpolicy.yaml" in paths
    assert ".agentit/observability/servicemonitor.yaml" in paths


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_create_onboarding_pr_error(mock_requests):
    """Verify API failures return an error dict."""
    import requests

    resp = MagicMock()
    resp.status_code = 404
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    resp.text = "Not Found"
    mock_requests.get.return_value = resp
    mock_requests.HTTPError = requests.HTTPError

    result = create_onboarding_pr(
        repo_url="https://github.com/org/nope.git",
        repo_name="nope",
        files=SAMPLE_FILES,
    )

    assert "error" in result


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_create_onboarding_pr_error_surfaces_response_body(mock_requests):
    """Regression test: the GitHub API's actual error body must be surfaced,
    not the generic `str(HTTPError)`.

    `requests.Response.__bool__` returns `self.ok`, which is False for any
    4xx/5xx status -- exactly the status range `raise_for_status()` raises
    for. A plain `MagicMock` doesn't reproduce this (its `__bool__` is True
    by default), so this test builds a real `requests.Response` to exercise
    the actual truthiness behavior that caused the bug: `if exc.response`
    was always falsy for HTTP errors, silently discarding the real API
    error body (e.g. "Resource not accessible by integration") in favor of
    the uninformative "404 Client Error: Not Found for url: ..." message.
    """
    import requests as real_requests

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/repos/org/my-app"):
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "abc123"}}
        return resp

    def mock_post(url, **kwargs):
        if "git/trees" in url:
            error_resp = real_requests.Response()
            error_resp.status_code = 404
            error_resp._content = b'{"message": "Resource not accessible by integration"}'
            raise real_requests.HTTPError(response=error_resp)
        resp = MagicMock()
        resp.status_code = 201
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post
    mock_requests.HTTPError = real_requests.HTTPError

    result = create_onboarding_pr(
        repo_url="https://github.com/org/my-app.git",
        repo_name="my-app",
        files=SAMPLE_FILES,
        branch_name="agentit/onboarding",
    )

    assert "error" in result
    assert "Resource not accessible by integration" in result["error"]
    assert "404 Client Error" not in result["error"]


def test_create_onboarding_pr_no_token():
    """Verify missing GITHUB_TOKEN returns error."""
    with patch.dict("os.environ", {}, clear=True):
        result = create_onboarding_pr(
            repo_url="https://github.com/org/test.git",
            repo_name="test",
            files=SAMPLE_FILES,
        )
    assert "error" in result
    assert "GITHUB_TOKEN" in result["error"]


# ── ensure_applicationset ────────────────────────────────────────────────
#
# Previously this shelled out to `oc apply -f -`, which meant it could only
# be tested by mocking subprocess.run (or not tested at all — see the
# incident described in test_portal.py's _override_store fixture). Now it's
# a check-then-create-or-patch against the kube API client.


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_creates_when_missing(mock_kube):
    mock_kube.get_custom_resource.return_value = None

    result = ensure_applicationset("https://github.com/org/agentit-gitops.git")

    assert result is True
    mock_kube.get_custom_resource.assert_called_once_with(
        "argoproj.io", "v1alpha1", "applicationsets", "agentit-managed-apps",
        namespace="openshift-gitops",
    )
    mock_kube.create_custom_resource.assert_called_once()
    args, _ = mock_kube.create_custom_resource.call_args
    assert args[:3] == ("argoproj.io", "v1alpha1", "applicationsets")
    assert args[3] == "openshift-gitops"
    assert args[4]["kind"] == "ApplicationSet"
    mock_kube.patch_custom_resource.assert_not_called()


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_patches_when_existing(mock_kube):
    mock_kube.get_custom_resource.return_value = {"metadata": {"name": "agentit-managed-apps"}}

    result = ensure_applicationset("https://github.com/org/agentit-gitops.git")

    assert result is True
    mock_kube.create_custom_resource.assert_not_called()
    mock_kube.patch_custom_resource.assert_called_once()
    args, _ = mock_kube.patch_custom_resource.call_args
    assert args[0] == "argoproj.io"
    assert args[3] == "agentit-managed-apps"
    assert args[4] == "openshift-gitops"


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_rejects_untrusted_domain(mock_kube):
    result = ensure_applicationset("https://evil.example.com/org/repo.git")

    assert result is False
    mock_kube.get_custom_resource.assert_not_called()
    mock_kube.create_custom_resource.assert_not_called()


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_returns_false_on_api_error(mock_kube):
    mock_kube.get_custom_resource.return_value = None
    mock_kube.create_custom_resource.side_effect = Exception("403 Forbidden")

    result = ensure_applicationset("https://github.com/org/agentit-gitops.git")

    assert result is False


# ── ensure_infra_repo ────────────────────────────────────────────────────
#
# Regression test for docs/code-review-2026-07-12.md item #8: the
# auto-created GitOps infra repo was created public, committing cluster
# manifests (namespace names, internal service names, schedule commands) to
# a world-readable repo.


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_infra_repo_creates_private_user_repo(mock_requests):
    get_resp = MagicMock()
    get_resp.status_code = 404  # repo doesn't exist yet
    mock_requests.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json.return_value = {"html_url": "https://github.com/org/agentit-gitops"}
    mock_requests.post.return_value = post_resp

    result = ensure_infra_repo("org", "agentit-gitops")

    assert result["created"] is True
    create_call = next(c for c in mock_requests.post.call_args_list if "/user/repos" in str(c))
    assert create_call.kwargs["json"]["private"] is True


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_infra_repo_creates_private_org_repo_on_fallback(mock_requests):
    """When /user/repos 422s (creating under an org, not the token's own user),
    the org fallback must also request a private repo."""
    get_resp = MagicMock()
    get_resp.status_code = 404
    mock_requests.get.return_value = get_resp

    user_post_resp = MagicMock()
    user_post_resp.status_code = 422

    org_post_resp = MagicMock()
    org_post_resp.status_code = 201
    org_post_resp.json.return_value = {"html_url": "https://github.com/myorg/agentit-gitops"}

    def post_side_effect(url, **kwargs):
        return org_post_resp if "/orgs/" in url else user_post_resp

    mock_requests.post.side_effect = post_side_effect

    result = ensure_infra_repo("myorg", "agentit-gitops")

    assert result["created"] is True
    org_call = next(c for c in mock_requests.post.call_args_list if "/orgs/" in str(c))
    assert org_call.kwargs["json"]["private"] is True
