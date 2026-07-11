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
