from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import requests

from agentit import kube

logger = logging.getLogger(__name__)

_API = "https://api.github.com"


def _get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN env var not set — cannot create PR")
    return token


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_owner_repo(repo_url: str) -> tuple[str, str]:
    path = repo_url.rstrip("/").removesuffix(".git")
    parts = path.split("/")
    return parts[-2], parts[-1]


def check_github_token() -> dict:
    """Cheap, real liveness check for the configured GitHub token, used by
    the Health page's Credentials section (``portal/helpers.py::
    get_credential_states()``).

    Calls ``GET /rate_limit`` -- unlike almost every other GitHub API
    endpoint this doesn't require any particular OAuth scope, and per
    GitHub's own docs it doesn't count against the caller's rate limit
    either, making it the cheapest possible way to confirm a token is both
    present and still accepted. Never raises: ``_get_token()``'s
    ``RuntimeError`` (unset ``GITHUB_TOKEN``) is caught here and reported
    as "missing" rather than propagating into a 500 on the Health page.

    Returns {"status": "missing"|"valid"|"invalid", "detail": str}.
    """
    try:
        token = _get_token()
    except RuntimeError:
        return {"status": "missing", "detail": "GITHUB_TOKEN is not set"}

    hdrs = _headers(token)
    try:
        resp = requests.get(f"{_API}/rate_limit", headers=hdrs, timeout=5)
    except Exception as exc:
        logger.warning("GitHub token liveness check failed: %s", exc)
        return {"status": "invalid", "detail": f"Could not reach the GitHub API: {exc}"}

    if resp.status_code == 200:
        return {"status": "valid", "detail": "Authenticated successfully via GET /rate_limit"}
    if resp.status_code in (401, 403):
        return {
            "status": "invalid",
            "detail": f"GitHub API returned {resp.status_code} -- token invalid or expired",
        }
    return {
        "status": "invalid",
        "detail": f"GitHub API returned unexpected status {resp.status_code}",
    }


def get_pr_status(pr_url: str) -> dict:
    """Check the merge status of a GitHub PR.

    Accepts full PR URLs (https://github.com/owner/repo/pull/N)
    or compare URLs (https://github.com/owner/repo/compare/branch).
    Returns {"state": "open"|"merged"|"closed"|"unknown", "merged_at": ..., "html_url": ...}.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        parts = pr_url.rstrip("/").split("/")

        if "/pull/" in pr_url and parts[-2] == "pull":
            owner, repo, pr_number = parts[-4], parts[-3], parts[-1]
            resp = requests.get(
                f"{_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=hdrs, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            state = "merged" if data.get("merged") else data.get("state", "unknown")
            return {
                "state": state,
                "merged_at": data.get("merged_at", ""),
                "html_url": data.get("html_url", pr_url),
                "title": data.get("title", ""),
                "body": data.get("body") or "",
                "labels": [lbl.get("name", "") for lbl in (data.get("labels") or [])],
                "created_at": data.get("created_at", ""),
            }

        if "/compare/" in pr_url:
            owner, repo = parts[-4], parts[-3]
            branch = parts[-1]
            resp = requests.get(
                f"{_API}/repos/{owner}/{repo}/pulls",
                headers=hdrs, timeout=10,
                params={"head": f"{owner}:{branch}", "state": "all", "per_page": 1},
            )
            resp.raise_for_status()
            prs = resp.json()
            if prs:
                pr = prs[0]
                state = "merged" if pr.get("merged_at") else pr.get("state", "unknown")
                return {
                    "state": state,
                    "merged_at": pr.get("merged_at", ""),
                    "html_url": pr.get("html_url", pr_url),
                    "title": pr.get("title", ""),
                    "body": pr.get("body") or "",
                    "labels": [lbl.get("name", "") for lbl in (pr.get("labels") or [])],
                    "created_at": pr.get("created_at", ""),
                }
            return {
                "state": "unknown", "merged_at": "", "html_url": pr_url,
                "title": "", "body": "", "labels": [], "created_at": "",
            }

        return {
            "state": "unknown", "merged_at": "", "html_url": pr_url,
            "title": "", "body": "", "labels": [], "created_at": "",
        }
    except Exception:
        logger.warning("Failed to check PR status for %s", pr_url, exc_info=True)
        return {
            "state": "unknown", "merged_at": "", "html_url": pr_url,
            "title": "", "body": "", "labels": [], "created_at": "",
        }


def get_commit_info(repo_url: str, sha: str) -> dict:
    """Fetch a single commit's message/author/URL from the GitHub API.

    Used by the deploy-status indicator (routes/health.py) to show *what's
    actually changing* in an in-progress or just-finished deployment -- the
    real commit message for the revision being built/deployed, not a
    fabricated placeholder. Returns `{}` on any failure (missing
    GITHUB_TOKEN, network error, unknown SHA, ...); callers must treat that
    as "no commit info available", never synthesize one.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        resp = requests.get(
            f"{_API}/repos/{owner}/{repo}/commits/{sha}",
            headers=hdrs, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        commit = data.get("commit", {})
        message = commit.get("message", "")
        return {
            "sha": data.get("sha", sha),
            "message": message.split("\n", 1)[0] if message else "",
            "author": commit.get("author", {}).get("name", ""),
            "html_url": data.get("html_url", ""),
        }
    except Exception:
        logger.warning("Failed to fetch commit info for %s@%s", repo_url, sha, exc_info=True)
        return {}


def create_onboarding_pr(
    repo_url: str,
    repo_name: str,
    files: list[dict],
    branch_name: str = "agentit/onboarding",
) -> dict:
    """Create a PR with onboarding manifests using the GitHub API.

    No git clone needed — uses the GitHub REST API directly.
    Requires GITHUB_TOKEN env var.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        resp = requests.get(f"{base_url}", headers=hdrs, timeout=10)
        resp.raise_for_status()
        default_branch = resp.json()["default_branch"]

        resp = requests.get(
            f"{base_url}/git/ref/heads/{default_branch}",
            headers=hdrs, timeout=10,
        )
        resp.raise_for_status()
        base_sha = resp.json()["object"]["sha"]

        tree_items = []
        for f in files:
            category = f["category"]
            filename = Path(f["path"]).name
            tree_items.append({
                "path": f".agentit/{category}/{filename}",
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            })

        resp = requests.post(
            f"{base_url}/git/trees",
            headers=hdrs, timeout=30,
            json={"base_tree": base_sha, "tree": tree_items},
        )
        resp.raise_for_status()
        tree_sha = resp.json()["sha"]

        resp = requests.post(
            f"{base_url}/git/commits",
            headers=hdrs, timeout=10,
            json={
                "message": "feat: add AgentIT enterprise onboarding manifests\n\nGenerated by AgentIT Enterprise Readiness Platform",
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )
        resp.raise_for_status()
        commit_sha = resp.json()["sha"]

        resp = requests.post(
            f"{base_url}/git/refs",
            headers=hdrs, timeout=10,
            json={"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
        )
        if resp.status_code == 422:
            requests.patch(
                f"{base_url}/git/refs/heads/{branch_name}",
                headers=hdrs, timeout=10,
                json={"sha": commit_sha, "force": True},
            ).raise_for_status()
        else:
            resp.raise_for_status()

        file_list = "\n".join(
            f"- `.agentit/{f['category']}/{Path(f['path']).name}` — {f['description']}"
            for f in files
        )
        pr_body = (
            "## AgentIT Enterprise Onboarding\n\n"
            "This PR adds enterprise-readiness manifests generated by AgentIT.\n\n"
            "### Generated Manifests\n"
            f"{file_list}\n\n"
            "> Generated by [AgentIT](https://github.com/alimobrem/AgentIT)"
        )

        resp = requests.post(
            f"{base_url}/pulls",
            headers=hdrs, timeout=10,
            json={
                "title": "AgentIT Enterprise Onboarding",
                "body": pr_body,
                "head": branch_name,
                "base": default_branch,
            },
        )
        if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
            return {"pr_url": f"{repo_url}/compare/{branch_name}", "branch": branch_name, "files_added": len(files)}
        resp.raise_for_status()

        pr_url = resp.json()["html_url"]
        return {"pr_url": pr_url, "branch": branch_name, "files_added": len(files)}

    except requests.HTTPError as exc:
        # `requests.Response.__bool__` returns `self.ok`, which is False for
        # every 4xx/5xx response -- exactly the case `raise_for_status()`
        # raises for. So `if exc.response else ...` is always falsy here and
        # silently discards the real GitHub API error body in favor of the
        # generic `str(exc)` (e.g. "404 Client Error: Not Found for url:
        # ..."). Check `is not None` instead so the actual response detail
        # surfaces to the caller.
        msg = exc.response.text if exc.response is not None else str(exc)
        logger.exception("GitHub API error creating PR")
        return {"error": f"GitHub API error: {msg[:200]}"}
    except Exception as exc:
        logger.exception("Failed to create PR")
        return {"error": str(exc)}


def _get_file_content_at_ref(base_url: str, hdrs: dict, path: str, ref: str) -> str | None:
    """Fetch a file's current text content at ``ref`` via the Contents API.

    Returns ``None`` when the file doesn't exist at ``ref`` (404) or its
    content can't be read/decoded (binary, oversized, transient API error)
    -- callers must treat ``None`` as "unknown/not present", i.e. always
    "different", never silently treat a lookup failure as "unchanged".
    """
    try:
        resp = requests.get(
            f"{base_url}/contents/{path}", headers=hdrs, timeout=10, params={"ref": ref},
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8")
    except Exception:
        return None


def _agent_content_unchanged(
    base_url: str, hdrs: dict, category: str, files: list[dict], default_branch: str,
) -> bool:
    """True only if every one of ``files`` is byte-identical to what's
    already at its destination path on the freshly-fetched ``default_branch``.

    This is the missing dedup check identified in the root-cause
    investigation of the recurring redundant-PR pattern (PRs #85/#89/#90/#91):
    self-improvement agents (codechange/cost/dependency) regenerate
    deterministic advisory content on every run and this function's caller
    used to unconditionally branch/commit/push/open-PR even when that exact
    content had already merged to `main` via an earlier run -- wasting
    review attention on a genuinely empty diff. Checked against a freshly
    fetched `default_branch` HEAD (not a stale local/cached ref), so this
    reflects the real current state of the target repo, not a snapshot from
    whenever this branch was first created.
    """
    for f in files:
        filename = Path(f["path"]).name
        target_path = f".agentit/{category}/{filename}"
        existing = _get_file_content_at_ref(base_url, hdrs, target_path, default_branch)
        if existing != f["content"]:
            return False
    return True


def create_agent_prs(
    repo_url: str,
    repo_name: str,
    agent_results: list[dict],
) -> list[dict]:
    """Create per-agent branches and PRs via the GitHub API.

    Each agent gets its own branch (agentit/{agent_name}) and PR with
    only that agent's generated files. Before committing anything, each
    agent's generated files are diffed against what's already at their
    destination path on the repo's current default branch (fetched fresh
    for this call, never a stale/cached ref) -- if every file is
    byte-identical, nothing is committed and no PR is opened for that agent
    (see `_agent_content_unchanged`'s docstring for why this check exists).

    agent_results: [{agent_name, category, files: [{path, content, description}]}]
    Returns: [{agent_name, pr_url, branch, error}] with {agent_name, skipped,
    reason} entries for agents whose content was already up to date.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        resp = requests.get(base_url, headers=hdrs, timeout=10)
        resp.raise_for_status()
        default_branch = resp.json()["default_branch"]

        resp = requests.get(
            f"{base_url}/git/ref/heads/{default_branch}",
            headers=hdrs, timeout=10,
        )
        resp.raise_for_status()
        base_sha = resp.json()["object"]["sha"]
    except Exception as exc:
        logger.exception("Failed to get repo info for per-agent PRs")
        return [{"agent_name": "setup", "error": str(exc)}]

    results: list[dict] = []

    for agent in agent_results:
        agent_name = agent["agent_name"]
        category = agent["category"]
        files = agent.get("files", [])
        if not files:
            continue

        if _agent_content_unchanged(base_url, hdrs, category, files, default_branch):
            logger.info(
                "agentit: %s manifests unchanged from %s -- skipping PR, nothing to commit",
                agent_name, default_branch,
            )
            results.append({
                "agent_name": agent_name,
                "skipped": True,
                "reason": f"content already matches {default_branch} -- no PR needed",
            })
            continue

        branch_name = f"agentit/{agent_name}"

        try:
            tree_items = []
            for f in files:
                filename = Path(f["path"]).name
                tree_items.append({
                    "path": f".agentit/{category}/{filename}",
                    "mode": "100644",
                    "type": "blob",
                    "content": f["content"],
                })

            resp = requests.post(
                f"{base_url}/git/trees",
                headers=hdrs, timeout=30,
                json={"base_tree": base_sha, "tree": tree_items},
            )
            resp.raise_for_status()
            tree_sha = resp.json()["sha"]

            resp = requests.post(
                f"{base_url}/git/commits",
                headers=hdrs, timeout=10,
                json={
                    "message": f"feat(agentit): {agent_name} — {len(files)} manifests for {repo_name}",
                    "tree": tree_sha,
                    "parents": [base_sha],
                },
            )
            resp.raise_for_status()
            commit_sha = resp.json()["sha"]

            resp = requests.post(
                f"{base_url}/git/refs",
                headers=hdrs, timeout=10,
                json={"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
            )
            if resp.status_code == 422:
                requests.patch(
                    f"{base_url}/git/refs/heads/{branch_name}",
                    headers=hdrs, timeout=10,
                    json={"sha": commit_sha, "force": True},
                ).raise_for_status()
            else:
                resp.raise_for_status()

            file_list = "\n".join(
                f"- `.agentit/{category}/{Path(f['path']).name}`"
                for f in files
            )
            pr_body = (
                f"## AgentIT: {agent_name}\n\n"
                f"Manifests generated by the **{agent_name}** agent.\n\n"
                f"### Files\n{file_list}\n\n"
                f"> Generated by [AgentIT](https://github.com/alimobrem/AgentIT)"
            )

            resp = requests.post(
                f"{base_url}/pulls",
                headers=hdrs, timeout=10,
                json={
                    "title": f"[AgentIT] {agent_name}: {len(files)} manifests for {repo_name}",
                    "body": pr_body,
                    "head": branch_name,
                    "base": default_branch,
                },
            )
            if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
                pr_url = f"{repo_url}/compare/{branch_name}"
            else:
                resp.raise_for_status()
                pr_url = resp.json()["html_url"]

            results.append({
                "agent_name": agent_name,
                "branch": branch_name,
                "pr_url": pr_url,
                "files_count": len(files),
            })

        except Exception as exc:
            logger.warning("Failed to create PR for agent %s: %s", agent_name, exc)
            results.append({
                "agent_name": agent_name,
                "error": str(exc),
            })

    return results


def merge_pr(pr_url: str) -> dict:
    """Merge a GitHub PR via the REST API.

    Used only by the ``gitops-pr-pending`` gate's approval path
    (``routes/gates.py::resolve_gate``): a human approving that gate *is*
    the merge action -- AgentIT itself never calls this to auto-merge on its
    own initiative, matching the design doc's explicit "a human always
    merges into a self-healing/pruning GitOps repo" posture (see
    docs/unified-apply-flow.md section (B)).
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        parts = pr_url.rstrip("/").split("/")
        if "/pull/" not in pr_url or len(parts) < 2 or parts[-2] != "pull":
            return {"error": f"not a PR URL: {pr_url}"}
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]
        resp = requests.put(
            f"{_API}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            headers=hdrs, timeout=15,
            json={"merge_method": "squash"},
        )
        if resp.status_code >= 400:
            return {"error": f"GitHub API error: {resp.text[:200]}"}
        data = resp.json()
        return {"merged": bool(data.get("merged", False)), "sha": data.get("sha", "")}
    except Exception as exc:
        logger.exception("Failed to merge PR %s", pr_url)
        return {"error": str(exc)}


def create_source_patch_pr(
    repo_url: str,
    repo_name: str,
    files: list[dict],
    branch_name: str = "agentit/codechange",
) -> dict:
    """Open a PR with a real patch against each file's actual location in
    the app's own repo -- fixes the pre-existing correctness gap where
    ``CodeChangeAgent``'s source-level fixes (Dockerfile, .gitignore, health
    endpoints, OTel/logging snippets) landed as loose copies under
    ``.agentit/codechange/*`` instead of a diff against the real target file
    (see docs/unified-apply-flow.md's "GitHub/source-repo changes -- real
    source patches" taxonomy row).

    Each ``files`` entry should carry a ``target_path`` (the real destination
    in the app's repo, set by ``CodeChangeAgent`` on each ``GeneratedFile``
    and threaded through by ``agents/orchestrator.py``'s target-path
    manifest) -- entries missing one fall back to their own ``path``
    unchanged, matching ``create_onboarding_pr``'s behavior for non-codechange
    callers.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        resp = requests.get(base_url, headers=hdrs, timeout=10)
        resp.raise_for_status()
        default_branch = resp.json()["default_branch"]

        resp = requests.get(
            f"{base_url}/git/ref/heads/{default_branch}",
            headers=hdrs, timeout=10,
        )
        resp.raise_for_status()
        base_sha = resp.json()["object"]["sha"]

        tree_items = []
        for f in files:
            target = f.get("target_path") or f["path"]
            tree_items.append({
                "path": target,
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            })

        resp = requests.post(
            f"{base_url}/git/trees",
            headers=hdrs, timeout=30,
            json={"base_tree": base_sha, "tree": tree_items},
        )
        resp.raise_for_status()
        tree_sha = resp.json()["sha"]

        resp = requests.post(
            f"{base_url}/git/commits",
            headers=hdrs, timeout=10,
            json={
                "message": f"fix(agentit): {len(files)} source-level change(s) for {repo_name}",
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )
        resp.raise_for_status()
        commit_sha = resp.json()["sha"]

        resp = requests.post(
            f"{base_url}/git/refs",
            headers=hdrs, timeout=10,
            json={"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
        )
        if resp.status_code == 422:
            requests.patch(
                f"{base_url}/git/refs/heads/{branch_name}",
                headers=hdrs, timeout=10,
                json={"sha": commit_sha, "force": True},
            ).raise_for_status()
        else:
            resp.raise_for_status()

        file_list = "\n".join(
            f"- `{f.get('target_path') or f['path']}` — {f.get('description', '')}"
            for f in files
        )
        pr_body = (
            "## AgentIT: source-level fixes\n\n"
            f"Real patch(es) against {len(files)} file(s) in this repo (not a loose copy).\n\n"
            f"### Files\n{file_list}\n\n"
            "> Generated by [AgentIT](https://github.com/alimobrem/AgentIT)"
        )

        resp = requests.post(
            f"{base_url}/pulls",
            headers=hdrs, timeout=10,
            json={
                "title": f"[AgentIT] {len(files)} source-level fix(es) for {repo_name}",
                "body": pr_body,
                "head": branch_name,
                "base": default_branch,
            },
        )
        if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
            return {"pr_url": f"{repo_url}/compare/{branch_name}", "branch": branch_name, "files_committed": len(files)}
        resp.raise_for_status()

        return {"pr_url": resp.json()["html_url"], "branch": branch_name, "files_committed": len(files)}

    except requests.HTTPError as exc:
        msg = exc.response.text if exc.response is not None else str(exc)
        logger.exception("GitHub API error creating source-patch PR")
        return {"error": f"GitHub API error: {msg[:200]}"}
    except Exception as exc:
        logger.exception("Failed to create source-patch PR")
        return {"error": str(exc)}


def commit_to_infra_repo(
    infra_repo_url: str,
    app_name: str,
    files: list[dict],
    branch_name: str | None = None,
) -> dict:
    """Commit onboarding manifests to the GitOps infra repo.

    Files are placed under apps/{app_name}/{category}/{filename}.
    Creates a branch and PR if branch_name is set, otherwise commits to main.

    Returns {"commit_url", "pr_url", "files_committed"} or {"error"}.
    """
    app_name = app_name.lower().replace("_", "-").replace(".", "-")
    branch_name = branch_name or f"agentit/{app_name}"

    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(infra_repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        resp = requests.get(base_url, headers=hdrs, timeout=10)
        resp.raise_for_status()
        default_branch = resp.json()["default_branch"]

        resp = requests.get(
            f"{base_url}/git/ref/heads/{default_branch}",
            headers=hdrs, timeout=10,
        )
        resp.raise_for_status()
        base_sha = resp.json()["object"]["sha"]

        tree_items = []
        for f in files:
            category = f.get("category", "misc")
            filename = Path(f["path"]).name
            tree_items.append({
                "path": f"apps/{app_name}/{category}/{filename}",
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            })

        resp = requests.post(
            f"{base_url}/git/trees",
            headers=hdrs, timeout=30,
            json={"base_tree": base_sha, "tree": tree_items},
        )
        resp.raise_for_status()
        tree_sha = resp.json()["sha"]

        resp = requests.post(
            f"{base_url}/git/commits",
            headers=hdrs, timeout=10,
            json={
                "message": f"feat(agentit): onboard {app_name} — {len(files)} manifests",
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )
        resp.raise_for_status()
        commit_sha = resp.json()["sha"]

        resp = requests.post(
            f"{base_url}/git/refs",
            headers=hdrs, timeout=10,
            json={"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
        )
        if resp.status_code == 422:
            requests.patch(
                f"{base_url}/git/refs/heads/{branch_name}",
                headers=hdrs, timeout=10,
                json={"sha": commit_sha, "force": True},
            ).raise_for_status()
        else:
            resp.raise_for_status()

        file_list = "\n".join(
            f"- `apps/{app_name}/{f.get('category', 'misc')}/{Path(f['path']).name}`"
            for f in files
        )
        pr_body = (
            f"## AgentIT: onboard {app_name}\n\n"
            f"Manifests committed to the GitOps infra repo.\n\n"
            f"### Files\n{file_list}\n\n"
            f"> Generated by [AgentIT](https://github.com/alimobrem/AgentIT)"
        )

        resp = requests.post(
            f"{base_url}/pulls",
            headers=hdrs, timeout=10,
            json={
                "title": f"[AgentIT] Onboard {app_name}",
                "body": pr_body,
                "head": branch_name,
                "base": default_branch,
            },
        )
        if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
            pr_url = f"{infra_repo_url}/compare/{branch_name}"
        else:
            resp.raise_for_status()
            pr_url = resp.json()["html_url"]

        return {
            "pr_url": pr_url,
            "commit_url": f"{infra_repo_url}/commit/{commit_sha}",
            "branch": branch_name,
            "files_committed": len(files),
        }

    except requests.HTTPError as exc:
        # See `create_onboarding_pr`'s except block above: `exc.response` is
        # falsy for every error response due to `Response.__bool__` returning
        # `self.ok`, so this must check `is not None` to actually use the
        # GitHub API's response body instead of always falling back to the
        # generic `str(exc)`.
        msg = exc.response.text if exc.response is not None else str(exc)
        logger.exception("GitHub API error committing to infra repo")
        return {"error": f"GitHub API error: {msg[:200]}"}
    except Exception as exc:
        logger.exception("Failed to commit to infra repo")
        return {"error": str(exc)}


_TRUSTED_GIT_DOMAINS = frozenset(
    d.strip() for d in os.environ.get("AGENTIT_TRUSTED_GIT_DOMAINS", "github.com,gitlab.com").split(",") if d.strip()
)


def is_trusted_git_host(repo_url: str) -> bool:
    """Whether ``repo_url``'s host is in ``AGENTIT_TRUSTED_GIT_DOMAINS``
    (default ``github.com,gitlab.com``) -- extracted from
    ``ensure_applicationset()`` (below) so the mandatory GitOps-registration
    gate (``routes/assessments.py``'s ``_resolve_mandatory_infra_repo_url()``)
    can validate a candidate infra repo URL up front, at Assess time, instead
    of only discovering post-hoc (at first-delivery time) that
    ``ensure_applicationset()`` will silently refuse to ever register it.
    """
    from urllib.parse import urlparse as _urlparse

    host = (_urlparse(repo_url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _TRUSTED_GIT_DOMAINS)


def ensure_applicationset(infra_repo_url: str) -> bool:
    """Ensure an Argo CD ApplicationSet exists for the infra repo."""
    if not is_trusted_git_host(infra_repo_url):
        logger.warning(
            "Skipping ApplicationSet: infra_repo_url host not in trusted domains %s: %s",
            _TRUSTED_GIT_DOMAINS, infra_repo_url,
        )
        return False

    name = "agentit-managed-apps"
    namespace = "openshift-gitops"
    appset = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "ApplicationSet",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "generators": [{
                "git": {
                    "repoURL": infra_repo_url,
                    "revision": "HEAD",
                    "directories": [
                    {"path": "apps/*"},
                    {"path": "apps/agentit", "exclude": True},
                ],
                },
            }],
            "template": {
                "metadata": {
                    "name": "managed-{{path.basename}}",
                    "namespace": "openshift-gitops",
                },
                "spec": {
                    "project": "default",
                    "source": {
                        "repoURL": infra_repo_url,
                        "targetRevision": "HEAD",
                        "path": "{{path}}",
                    },
                    "destination": {
                        "server": "https://kubernetes.default.svc",
                        "namespace": "{{path.basename}}",
                    },
                    "syncPolicy": {
                        "automated": {"selfHeal": True, "prune": True},
                        "syncOptions": ["CreateNamespace=true"],
                    },
                },
            },
        },
    }

    try:
        existing = kube.get_custom_resource(
            "argoproj.io", "v1alpha1", "applicationsets", name, namespace=namespace,
        )
        if existing is None:
            kube.create_custom_resource("argoproj.io", "v1alpha1", "applicationsets", namespace, appset)
        else:
            kube.patch_custom_resource("argoproj.io", "v1alpha1", "applicationsets", name, namespace, appset)
        logger.info("ApplicationSet ensured for %s", infra_repo_url)
        return True
    except Exception as exc:
        logger.warning("ApplicationSet apply error: %s", exc)
    return False


def ensure_infra_repo(owner: str, repo_name: str = "agentit-gitops") -> dict:
    """Create a GitOps infra repo if it doesn't exist. Returns {"repo_url"} or {"error"}.

    Checks if the repo exists first under ``owner``. If not, creates a private
    repo via ``/user/repos`` (authenticated token owner). When that 422s
    because the name already exists under the token user, reuses that repo
    instead of failing — this is the Register-for-GitOps path for third-party
    app owners (e.g. ``octocat/Hello-World``) where ``/orgs/{owner}/repos``
    is not permitted. ``apps/.gitkeep`` is written to the repo that was
    actually created/reused, not blindly to ``owner/repo_name``.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        repo_url = f"https://github.com/{owner}/{repo_name}"

        resp = requests.get(f"{_API}/repos/{owner}/{repo_name}", headers=hdrs, timeout=10)
        if resp.status_code == 200:
            return {"repo_url": resp.json().get("html_url", repo_url), "created": False}

        # Private: this repo holds cluster manifests (namespace names, internal
        # service names, schedule commands) that shouldn't be world-readable.
        resp = requests.post(
            f"{_API}/user/repos",
            headers=hdrs, timeout=10,
            json={
                "name": repo_name,
                "description": "AgentIT GitOps infrastructure — managed by AgentIT agents",
                "private": True,
                "auto_init": True,
            },
        )
        if resp.status_code == 422:
            # Already exists under the authenticated user — reuse it. Fall
            # back to org create only when the user-owned repo is missing.
            me = requests.get(f"{_API}/user", headers=hdrs, timeout=10)
            login = me.json().get("login") if me.ok else None
            if login:
                existing = requests.get(
                    f"{_API}/repos/{login}/{repo_name}", headers=hdrs, timeout=10,
                )
                if existing.status_code == 200:
                    return {
                        "repo_url": existing.json().get(
                            "html_url", f"https://github.com/{login}/{repo_name}"
                        ),
                        "created": False,
                    }
            resp = requests.post(
                f"{_API}/orgs/{owner}/repos",
                headers=hdrs, timeout=10,
                json={
                    "name": repo_name,
                    "description": "AgentIT GitOps infrastructure — managed by AgentIT agents",
                    "private": True,
                    "auto_init": True,
                },
            )
        resp.raise_for_status()
        created_url = resp.json().get("html_url", repo_url)
        # /user/repos always creates under the token login — write gitkeep
        # there, not to the (possibly third-party) requested owner path.
        actual_owner, actual_repo = _parse_owner_repo(created_url)

        requests.put(
            f"{_API}/repos/{actual_owner}/{actual_repo}/contents/apps/.gitkeep",
            headers=hdrs, timeout=10,
            json={
                "message": "chore: initialize apps directory for managed applications",
                "content": base64.b64encode(b"").decode(),
            },
        )

        logger.info("Created infra repo: %s", created_url)
        return {"repo_url": created_url, "created": True}

    except Exception as exc:
        logger.exception("Failed to create infra repo")
        return {"error": str(exc)}


def ensure_webhook(repo_url: str, webhook_url: str) -> dict:
    """Ensure a GitHub push webhook exists on the repo pointing to our endpoint.

    Idempotent — if a webhook with the same URL already exists, returns it.
    Returns {"id": webhook_id, "created": bool} or {"error": str}.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        resp = requests.get(f"{base_url}/hooks", headers=hdrs, timeout=10)
        resp.raise_for_status()
        for hook in resp.json():
            if hook.get("config", {}).get("url", "") == webhook_url:
                logger.info("Webhook already exists on %s/%s (id=%s)", owner, repo, hook["id"])
                return {"id": hook["id"], "created": False}

        resp = requests.post(
            f"{base_url}/hooks",
            headers=hdrs, timeout=10,
            json={
                "name": "web",
                "active": True,
                "events": ["push"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "insecure_ssl": "0",
                },
            },
        )
        resp.raise_for_status()
        hook_id = resp.json()["id"]
        logger.info("Created push webhook on %s/%s (id=%s) → %s", owner, repo, hook_id, webhook_url)
        return {"id": hook_id, "created": True}

    except requests.HTTPError as exc:
        # See `create_onboarding_pr`'s except block above: same
        # `Response.__bool__` gotcha, fixed the same way.
        msg = exc.response.text if exc.response is not None else str(exc)
        logger.warning("Failed to create webhook on %s: %s", repo_url, msg[:200])
        return {"error": f"GitHub API error: {msg[:200]}"}
    except Exception as exc:
        logger.warning("Failed to ensure webhook on %s: %s", repo_url, exc)
        return {"error": str(exc)}
