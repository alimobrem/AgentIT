from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

from agentit.portal.github_pr import (
    commit_to_infra_repo,
    create_onboarding_pr,
    ensure_applicationset,
    ensure_infra_repo,
    ensure_webhook,
)


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
def test_create_onboarding_pr_already_open_returns_the_real_pr_url_not_a_compare_link(mock_requests):
    """GitHub's `POST .../pulls` 422s "pull request already exists" when
    `branch_name` already has one open (a second commit to the same branch
    before the first PR merged/closed). Used to fall back to an inert
    `{repo_url}/compare/{branch_name}` link -- clickable, but never
    resolvable to a real lifecycle (pr_tracking.py's annotate_lifecycle()
    stays stuck on "Unknown" forever for it, since there's no PR number to
    look up). Must look up and return the real, already-open PR's own
    `html_url` instead."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/repos/org/my-app"):
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "abc123"}}
        elif url.endswith("/pulls"):
            assert kwargs["params"]["head"] == "org:agentit/onboarding"
            resp.json.return_value = [{"html_url": "https://github.com/org/my-app/pull/17"}]
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/pulls"):
            resp.status_code = 422
            resp.text = "A pull request already exists for org:agentit/onboarding."
            return resp
        resp.status_code = 201
        if "git/trees" in url:
            resp.json.return_value = {"sha": "tree456"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "commit789"}
        elif "git/refs" in url:
            resp.json.return_value = {"ref": "refs/heads/agentit/onboarding"}
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post

    result = create_onboarding_pr(
        repo_url="https://github.com/org/my-app.git",
        repo_name="my-app",
        files=SAMPLE_FILES,
        branch_name="agentit/onboarding",
    )

    assert result["pr_url"] == "https://github.com/org/my-app/pull/17"
    assert "compare" not in result["pr_url"]


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_create_onboarding_pr_already_open_falls_back_to_compare_link_if_lookup_fails(mock_requests):
    """The existing-PR lookup itself is best-effort -- a failure there must
    still return a clickable (if unresolvable) link, never turn an
    otherwise-successful commit into an error."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/repos/org/my-app"):
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "abc123"}}
        elif url.endswith("/pulls"):
            resp.json.return_value = []
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/pulls"):
            resp.status_code = 422
            resp.text = "A pull request already exists for org:agentit/onboarding."
            return resp
        resp.status_code = 201
        if "git/trees" in url:
            resp.json.return_value = {"sha": "tree456"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "commit789"}
        elif "git/refs" in url:
            resp.json.return_value = {"ref": "refs/heads/agentit/onboarding"}
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post

    result = create_onboarding_pr(
        repo_url="https://github.com/org/my-app.git",
        repo_name="my-app",
        files=SAMPLE_FILES,
        branch_name="agentit/onboarding",
    )

    assert result["pr_url"] == "https://github.com/org/my-app.git/compare/agentit/onboarding"


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_commit_to_infra_repo_already_open_returns_the_real_pr_url_not_a_compare_link(mock_requests):
    """Same fix as `create_onboarding_pr`'s -- and the one actually reported
    live (a "Cluster config" delivery card kept rendering an inert
    /compare/ link with a permanent "Unknown" lifecycle badge). Also
    exercises `git/refs`' own, unrelated 422 (branch ref already exists,
    force-updated) alongside `/pulls`' 422 (PR already exists) in the same
    call -- they must not be confused with each other."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/repos/org/agentit-gitops"):
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "abc123"}}
        elif url.endswith("/pulls"):
            assert kwargs["params"]["head"] == "org:agentit/pinky"
            resp.json.return_value = [{"html_url": "https://github.com/org/agentit-gitops/pull/22"}]
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/pulls"):
            resp.status_code = 422
            resp.text = "A pull request already exists for org:agentit/pinky."
            return resp
        resp.status_code = 201
        if "git/trees" in url:
            resp.json.return_value = {"sha": "tree456"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "commit789"}
        elif "git/refs" in url:
            # The branch ref already exists too -- a real, separate 422
            # this same call also has to handle (force-update via PATCH),
            # unrelated to the /pulls one this test is about.
            resp.status_code = 422
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post
    mock_requests.patch.return_value = MagicMock(status_code=200)

    result = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/agentit-gitops",
        app_name="pinky",
        files=SAMPLE_FILES,
        branch_name="agentit/pinky",
    )

    assert result["pr_url"] == "https://github.com/org/agentit-gitops/pull/22"
    assert "compare" not in result["pr_url"]


def test_commit_to_infra_repo_refuses_apps_agentit_dead_letter():
    """Belt-and-suspenders: never open orphan PRs under apps/agentit/
    (AppSet excludes that path; Application agentit syncs AgentIT.git chart/).
    See docs/architecture-agentit-vs-fleet-gitops.md."""
    result = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/agentit-gitops",
        app_name="agentit",
        files=SAMPLE_FILES,
    )
    assert "error" in result
    assert "apps/agentit" in result["error"]
    assert "AgentIT.git" in result["error"] or "chart/" in result["error"]


def test_commit_to_infra_repo_refuses_empty_files_list():
    result = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/agentit-gitops",
        app_name="pinky",
        files=[],
    )
    assert result.get("skipped") is True
    assert "no files" in result["reason"]


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


# create_agent_prs / Per-Agent PRs removed as a product path (skills-primary
# simplification). Infra-repo dedup below remains — Scan/auto_delivery uses it.

# ── commit_to_infra_repo — dedup against current default branch ─────────
#
# 2026-07-20 (unify-scan-onboard-chain): `commit_to_infra_repo()` is the
# PRIMARY onboarding delivery mechanism (every GitOps-registered app's
# cluster-config/CI-CD-shared-namespace manifests route through it via
# delivery.py's `_deliver_via_gitops_pr()`). Proves `_infra_repo_content_
# unchanged()` prevents a redundant commit/PR on Scan re-runs.

_NETPOL_CONTENT = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n"


def _infra_repo_files(content: str = _NETPOL_CONTENT) -> list[dict]:
    return [{"category": "skills", "path": "netpol.yaml", "content": content, "description": "network policy"}]


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_commit_to_infra_repo_skips_when_content_already_matches_default_branch(mock_requests):
    """Byte-identical generated manifests vs. what's already at their
    destination path (apps/{app}/{category}/{filename}) on the (freshly
    fetched) default branch must skip the commit/PR entirely -- no
    mutating call is made at all."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/repos/org/steady-app-gitops"):
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.json.return_value = {"object": {"sha": "sha-main-1"}}
        elif url.endswith("/contents/apps/steady-app/skills/netpol.yaml"):
            resp.json.return_value = {"content": base64.b64encode(_NETPOL_CONTENT.encode()).decode()}
        return resp

    mock_requests.get.side_effect = mock_get

    result = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/steady-app-gitops",
        app_name="steady-app",
        files=_infra_repo_files(),
    )

    assert result == {
        "skipped": True,
        "reason": "content already matches main -- no PR needed",
    }
    mock_requests.post.assert_not_called()


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_commit_to_infra_repo_commits_when_content_differs(mock_requests):
    """When the file doesn't exist yet on the default branch (or differs),
    the normal branch/commit/PR flow must still run unchanged."""
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/org/fresh-app-gitops"):
            resp.status_code = 200
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.status_code = 200
            resp.json.return_value = {"object": {"sha": "sha-main-1"}}
        elif url.endswith("/contents/apps/fresh-app/skills/netpol.yaml"):
            resp.status_code = 404
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        if "git/trees" in url:
            resp.json.return_value = {"sha": "tree-1"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "commit-1"}
        elif "git/refs" in url:
            resp.json.return_value = {"ref": "refs/heads/agentit/fresh-app"}
        elif "/pulls" in url:
            resp.json.return_value = {"html_url": "https://github.com/org/fresh-app-gitops/pull/1"}
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post

    result = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/fresh-app-gitops",
        app_name="fresh-app",
        files=_infra_repo_files(),
    )

    assert result["pr_url"] == "https://github.com/org/fresh-app-gitops/pull/1"
    assert "skipped" not in result
    assert len([c for c in mock_requests.post.call_args_list if "git/trees" in str(c)]) == 1


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_commit_to_infra_repo_second_identical_run_noops_after_first_merges(mock_requests):
    """The exact steady-state sequence this dedup exists for: run 1 commits
    genuinely new manifests and opens a PR; that PR merges to the default
    branch (which also advances to a new commit sha, proving the dedup
    check reads a live, current ref rather than a cached/stale one); a
    later automatic re-onboard (a cadence-triggered re-scan of an app
    that's already onboarded and unchanged) regenerates the *same*
    manifests and must no-op instead of opening a second, redundant PR."""
    main_state = {"content": None, "sha": "sha-main-1"}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/repos/org/steady-app2-gitops"):
            resp.status_code = 200
            resp.json.return_value = {"default_branch": "main"}
        elif "git/ref/heads/main" in url:
            resp.status_code = 200
            resp.json.return_value = {"object": {"sha": main_state["sha"]}}
        elif url.endswith("/contents/apps/steady-app2/skills/netpol.yaml"):
            if main_state["content"] is None:
                resp.status_code = 404
            else:
                resp.status_code = 200
                resp.json.return_value = {"content": base64.b64encode(main_state["content"].encode()).decode()}
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        if "git/trees" in url:
            resp.json.return_value = {"sha": "tree-1"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "commit-1"}
        elif "git/refs" in url:
            resp.json.return_value = {"ref": "refs/heads/agentit/steady-app2"}
        elif "/pulls" in url:
            resp.json.return_value = {"html_url": "https://github.com/org/steady-app2-gitops/pull/1"}
        return resp

    mock_requests.get.side_effect = mock_get
    mock_requests.post.side_effect = mock_post

    # Run 1: nothing on main yet -- commits and opens a real PR.
    run_1 = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/steady-app2-gitops",
        app_name="steady-app2",
        files=_infra_repo_files(),
    )
    assert run_1["pr_url"] == "https://github.com/org/steady-app2-gitops/pull/1"
    assert mock_requests.post.call_count >= 1

    # The PR from run 1 merges -- main now has the generated content and a
    # new HEAD sha.
    main_state["content"] = _NETPOL_CONTENT
    main_state["sha"] = "sha-main-2"
    mock_requests.post.reset_mock()

    # A later automatic re-onboard regenerates byte-identical content --
    # must no-op, not open a second PR.
    run_2 = commit_to_infra_repo(
        infra_repo_url="https://github.com/org/steady-app2-gitops",
        app_name="steady-app2",
        files=_infra_repo_files(),
    )
    assert run_2 == {
        "skipped": True,
        "reason": "content already matches main -- no PR needed",
    }
    mock_requests.post.assert_not_called()


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
    directory = args[4]["spec"]["template"]["spec"]["source"]["directory"]
    assert directory["recurse"] is True
    assert "*.yaml" in directory["include"]
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
    directory = args[5]["spec"]["template"]["spec"]["source"]["directory"]
    assert directory["recurse"] is True
    assert "*.yml" in directory["include"]


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


# ── ensure_applicationset: bring-your-own-GitOps-repo additive fix ──────
#
# Previously this REPLACED spec.generators with one entry for whatever
# infra_repo_url was passed most recently -- onboarding app A into a
# different repo than app B silently orphaned Argo sync for B. Now it's
# read-merge-write: a distinct infra_repo_url gets its own generator
# entry APPENDED, never replacing what's already there. The shared
# template's source.repoURL is also now a `{{values.repoURL}}` generator-
# values reference rather than a hardcoded literal, so each generated
# Application still resolves to the correct repo regardless of how many
# generators are present.


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_second_distinct_repo_is_appended_not_replacing_first(mock_kube):
    """The core landmine fix: onboarding a second app against a different
    custom GitOps repo must not remove the first repo's generator entry."""
    existing_appset = {
        "metadata": {"name": "agentit-managed-apps"},
        "spec": {
            "generators": [
                {"git": {
                    "repoURL": "https://github.com/org/agentit-gitops",
                    "revision": "HEAD",
                    "directories": [{"path": "apps/*"}],
                    "values": {"repoURL": "https://github.com/org/agentit-gitops"},
                }},
            ],
            "template": {"spec": {"source": {"repoURL": "{{values.repoURL}}"}}},
        },
    }
    mock_kube.get_custom_resource.return_value = existing_appset

    result = ensure_applicationset("https://github.com/customorg/custom-gitops")

    assert result is True
    mock_kube.create_custom_resource.assert_not_called()
    mock_kube.patch_custom_resource.assert_called_once()
    args, _ = mock_kube.patch_custom_resource.call_args
    generators = args[5]["spec"]["generators"]
    repo_urls = {g["git"]["repoURL"] for g in generators}
    assert repo_urls == {
        "https://github.com/org/agentit-gitops",
        "https://github.com/customorg/custom-gitops",
    }
    # The shared template must reference the generator's own values, never
    # a hardcoded literal -- otherwise every generated Application would
    # sync from whichever repo was passed most recently, regardless of
    # which generator actually produced it.
    assert args[5]["spec"]["template"]["spec"]["source"]["repoURL"] == "{{values.repoURL}}"


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_reregistering_same_repo_is_idempotent_noop(mock_kube):
    """Calling this again for a repo already present must not duplicate
    its generator entry."""
    existing_appset = {
        "metadata": {"name": "agentit-managed-apps"},
        "spec": {
            "generators": [
                {"git": {
                    "repoURL": "https://github.com/org/agentit-gitops",
                    "values": {"repoURL": "https://github.com/org/agentit-gitops"},
                }},
            ],
        },
    }
    mock_kube.get_custom_resource.return_value = existing_appset

    result = ensure_applicationset("https://github.com/org/agentit-gitops")

    assert result is True
    args, _ = mock_kube.patch_custom_resource.call_args
    generators = args[5]["spec"]["generators"]
    assert len(generators) == 1
    assert generators[0]["git"]["repoURL"] == "https://github.com/org/agentit-gitops"


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_heals_pre_fix_entry_missing_values_field(mock_kube):
    """Live-cluster-caught regression: a generator entry created by the
    OLD (pre-additive-fix) code has no `git.values.repoURL` at all. Once
    the shared template reads `{{values.repoURL}}` instead of a hardcoded
    literal, an entry left as-is (rather than normalized) would resolve
    an empty/unset repoURL on its next Argo reconcile -- confirmed live:
    `managed-pinky`'s Application briefly dropped to sync status
    "Unknown" with a literal, unresolved `{{values.repoURL}}` in
    `spec.source.repoURL` before this normalization was added. Every call
    must re-sync an already-present entry to the current shape, not just
    skip it as "already there"."""
    pre_fix_appset = {
        "metadata": {"name": "agentit-managed-apps"},
        "spec": {
            "generators": [
                {"git": {
                    "repoURL": "https://github.com/alimobrem/agentit-gitops",
                    "revision": "HEAD",
                    "directories": [
                        {"path": "apps/*"},
                        {"path": "apps/agentit", "exclude": True},
                    ],
                    # No `values` key -- the exact pre-fix shape.
                }},
            ],
        },
    }
    mock_kube.get_custom_resource.return_value = pre_fix_appset

    result = ensure_applicationset("https://github.com/alimobrem/agentit-gitops")

    assert result is True
    args, _ = mock_kube.patch_custom_resource.call_args
    generators = args[5]["spec"]["generators"]
    assert len(generators) == 1
    assert generators[0]["git"]["values"] == {
        "repoURL": "https://github.com/alimobrem/agentit-gitops",
    }


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_new_generator_carries_own_values_repo_url(mock_kube):
    """Each generator entry must set its own `git.values.repoURL` -- the
    mechanism the shared `{{values.repoURL}}` template resolves through."""
    mock_kube.get_custom_resource.return_value = None

    ensure_applicationset("https://github.com/customorg/custom-gitops")

    args, _ = mock_kube.create_custom_resource.call_args
    generator = args[4]["spec"]["generators"][0]
    assert generator["git"]["values"] == {"repoURL": "https://github.com/customorg/custom-gitops"}
    assert args[4]["spec"]["template"]["spec"]["source"]["repoURL"] == "{{values.repoURL}}"


@patch("agentit.portal.github_pr.kube")
def test_ensure_applicationset_preserves_third_repo_when_adding_a_fourth(mock_kube):
    """Three-repo scenario -- makes sure the merge logic scales past two
    entries, not just a special-cased pair."""
    existing_appset = {
        "metadata": {"name": "agentit-managed-apps"},
        "spec": {
            "generators": [
                {"git": {"repoURL": "https://github.com/org/agentit-gitops",
                          "values": {"repoURL": "https://github.com/org/agentit-gitops"}}},
                {"git": {"repoURL": "https://github.com/customorg1/custom-gitops",
                          "values": {"repoURL": "https://github.com/customorg1/custom-gitops"}}},
            ],
        },
    }
    mock_kube.get_custom_resource.return_value = existing_appset

    result = ensure_applicationset("https://github.com/customorg2/another-gitops")

    assert result is True
    args, _ = mock_kube.patch_custom_resource.call_args
    repo_urls = {g["git"]["repoURL"] for g in args[5]["spec"]["generators"]}
    assert repo_urls == {
        "https://github.com/org/agentit-gitops",
        "https://github.com/customorg1/custom-gitops",
        "https://github.com/customorg2/another-gitops",
    }


# ── expected_managed_apps_repo_url ───────────────────────────────────────
#
# DriftDetector's ApplicationSet self-heal (2026-07-18) needs a real,
# non-hardcoded "what's correct" value -- these prove it's genuinely derived
# through this module's own owner-resolution routine, not a second literal
# URL that could silently drift out of sync with `ensure_infra_repo()`'s
# actual convention.


def test_expected_managed_apps_repo_url_follows_infra_repo_naming_convention():
    from agentit.portal.github_pr import expected_managed_apps_repo_url

    url = expected_managed_apps_repo_url()

    assert url == "https://github.com/alimobrem/agentit-gitops"


@patch("agentit.portal.github_pr._parse_owner_repo")
def test_expected_managed_apps_repo_url_is_derived_via_parse_owner_repo(mock_parse):
    """Regression guard against silently hardcoding the final URL:
    swapping the owner-resolution routine's return value must change the
    computed expected URL -- proving the function actually calls through
    it instead of just returning a literal string."""
    from agentit.portal.github_pr import expected_managed_apps_repo_url

    mock_parse.return_value = ("someone-else", "AgentIT")

    url = expected_managed_apps_repo_url()

    assert url == "https://github.com/someone-else/agentit-gitops"
    mock_parse.assert_called_once_with("https://github.com/alimobrem/AgentIT")


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


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_infra_repo_reuses_token_user_repo_on_422(mock_requests):
    """Register for GitOps against a third-party app owner (octocat/...) must
    reuse the token user's existing agentit-gitops instead of failing after
    /user/repos 422 — that silent failure made the portal button look dead."""
    owner_get = MagicMock()
    owner_get.status_code = 404

    me_get = MagicMock()
    me_get.status_code = 200
    me_get.ok = True
    me_get.json.return_value = {"login": "agentit-bot"}

    user_repo_get = MagicMock()
    user_repo_get.status_code = 200
    user_repo_get.json.return_value = {
        "html_url": "https://github.com/agentit-bot/agentit-gitops",
    }

    def get_side_effect(url, **kwargs):
        if url.endswith("/user"):
            return me_get
        if "/repos/agentit-bot/agentit-gitops" in url:
            return user_repo_get
        return owner_get

    mock_requests.get.side_effect = get_side_effect

    user_post_resp = MagicMock()
    user_post_resp.status_code = 422
    mock_requests.post.return_value = user_post_resp

    result = ensure_infra_repo("octocat", "agentit-gitops")

    assert result == {
        "repo_url": "https://github.com/agentit-bot/agentit-gitops",
        "created": False,
    }
    assert not any("/orgs/" in str(c) for c in mock_requests.post.call_args_list)


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_infra_repo_writes_gitkeep_to_created_owner(mock_requests):
    """/user/repos creates under the token login — gitkeep must target that
    owner, not the (possibly third-party) requested app owner."""
    get_resp = MagicMock()
    get_resp.status_code = 404
    mock_requests.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json.return_value = {"html_url": "https://github.com/agentit-bot/agentit-gitops"}
    mock_requests.post.return_value = post_resp

    put_resp = MagicMock()
    put_resp.status_code = 201
    mock_requests.put.return_value = put_resp

    result = ensure_infra_repo("octocat", "agentit-gitops")

    assert result["created"] is True
    put_url = mock_requests.put.call_args.args[0]
    assert "/repos/agentit-bot/agentit-gitops/contents/apps/.gitkeep" in put_url
    assert "/repos/octocat/" not in put_url


# ── ensure_webhook ───────────────────────────────────────────────────────
#
# Regression test for the live "Awaiting verification" investigation: GitHub
# webhook delivery to a self-signed-ingress-cert cluster (the OpenShift
# default) fails every attempt with "certificate signed by unknown
# authority" when `insecure_ssl` is hardcoded to "0" -- confirmed live via
# `gh api repos/.../hooks/{id}/deliveries` showing a 100% failure rate for
# AgentIT's own push webhook. `check_pending_delivery_verifications()` only
# ever runs from a successfully-delivered push webhook, so this silently
# starves it and leaves deliveries stuck "Awaiting verification" forever.


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_webhook_defaults_to_verifying_tls(mock_requests):
    """No `AGENTIT_WEBHOOK_INSECURE_SSL` override -> the secure default
    ("0", verify TLS) is sent, unchanged from today's behavior."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = []
    mock_requests.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json.return_value = {"id": 42}
    mock_requests.post.return_value = post_resp

    os.environ.pop("AGENTIT_WEBHOOK_INSECURE_SSL", None)
    os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
    result = ensure_webhook(
        "https://github.com/org/my-app.git", "https://agentit.example.com/api/webhook/github-push",
    )

    assert result == {"id": 42, "created": True, "updated": False}
    assert mock_requests.post.call_args.kwargs["json"]["config"]["insecure_ssl"] == "0"
    assert "secret" not in mock_requests.post.call_args.kwargs["json"]["config"]


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123", "AGENTIT_WEBHOOK_INSECURE_SSL": "1"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_webhook_insecure_ssl_override_for_self_signed_clusters(mock_requests):
    """`AGENTIT_WEBHOOK_INSECURE_SSL=1` (set for clusters using a self-signed
    ingress cert) must register the hook with `insecure_ssl: "1"`, so GitHub
    actually delivers push events instead of failing TLS verification."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = []
    mock_requests.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.json.return_value = {"id": 43}
    mock_requests.post.return_value = post_resp

    result = ensure_webhook(
        "https://github.com/org/my-app.git", "https://agentit.example.com/api/webhook/github-push",
    )

    assert result == {"id": 43, "created": True, "updated": False}
    assert mock_requests.post.call_args.kwargs["json"]["config"]["insecure_ssl"] == "1"


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"}, clear=False)
@patch("agentit.portal.github_pr.requests")
def test_ensure_webhook_idempotent_skips_create_on_existing_url(mock_requests):
    """A hook already registered at the exact same URL must not be
    recreated -- unaffected by the `insecure_ssl` change above."""
    os.environ.pop("AGENTIT_WEBHOOK_INSECURE_SSL", None)
    os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = [
        {
            "id": 7,
            "config": {
                "url": "https://agentit.example.com/api/webhook/github-push",
                "insecure_ssl": "0",
            },
        },
    ]
    mock_requests.get.return_value = get_resp

    result = ensure_webhook(
        "https://github.com/org/my-app.git", "https://agentit.example.com/api/webhook/github-push",
    )

    assert result == {"id": 7, "created": False, "updated": False}
    mock_requests.post.assert_not_called()
    mock_requests.patch.assert_not_called()


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123", "AGENTIT_WEBHOOK_INSECURE_SSL": "1"})
@patch("agentit.portal.github_pr.requests")
def test_ensure_webhook_normalizes_http_url_and_deletes_stale_http_hook(mock_requests):
    """Pinky 2026-07-22: a stale http:// hook 302'd forever on the OpenShift
    Route while a sibling https:// hook existed. ensure_webhook must upgrade
    the requested URL and delete the http duplicate."""
    list1 = MagicMock()
    list1.status_code = 200
    list1.json.return_value = [
        {
            "id": 1,
            "config": {
                "url": "http://agentit.example.com/api/webhook/github-push",
                "insecure_ssl": "0",
            },
        },
        {
            "id": 2,
            "config": {
                "url": "https://agentit.example.com/api/webhook/github-push",
                "insecure_ssl": "1",
            },
        },
    ]
    list2 = MagicMock()
    list2.status_code = 200
    list2.json.return_value = [
        {
            "id": 2,
            "config": {
                "url": "https://agentit.example.com/api/webhook/github-push",
                "insecure_ssl": "1",
            },
        },
    ]
    mock_requests.get.side_effect = [list1, list2]
    mock_requests.delete.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)

    result = ensure_webhook(
        "https://github.com/org/pinky.git",
        "http://agentit.example.com/api/webhook/github-push",
    )

    assert result == {"id": 2, "created": False, "updated": False}
    mock_requests.delete.assert_called_once()
    assert mock_requests.delete.call_args.args[0].endswith("/hooks/1")
    mock_requests.post.assert_not_called()


@patch.dict(
    "os.environ",
    {
        "GITHUB_TOKEN": "ghp_test123",
        "AGENTIT_WEBHOOK_INSECURE_SSL": "1",
        "GITHUB_WEBHOOK_SECRET": "s3cret",
    },
)
@patch("agentit.portal.github_pr.requests")
def test_ensure_webhook_patches_insecure_ssl_and_missing_secret(mock_requests):
    """https hook already registered with insecure_ssl=0 and no secret must
    be PATCHed — not left as a silent TLS/HMAC failure on the next push."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = [
        {
            "id": 9,
            "config": {
                "url": "https://agentit.example.com/api/webhook/github-push",
                "insecure_ssl": "0",
            },
        },
    ]
    mock_requests.get.return_value = get_resp
    patch_resp = MagicMock()
    patch_resp.status_code = 200
    patch_resp.raise_for_status = lambda: None
    mock_requests.patch.return_value = patch_resp

    result = ensure_webhook(
        "https://github.com/org/my-app.git",
        "https://agentit.example.com/api/webhook/github-push",
    )

    assert result == {"id": 9, "created": False, "updated": True}
    cfg = mock_requests.patch.call_args.kwargs["json"]["config"]
    assert cfg["insecure_ssl"] == "1"
    assert cfg["secret"] == "s3cret"
    mock_requests.post.assert_not_called()
