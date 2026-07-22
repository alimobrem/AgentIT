from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
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


def _find_existing_pr_url(base_url: str, hdrs: dict, owner: str, branch_name: str, fallback_url: str) -> str:
    """GitHub's ``POST .../pulls`` returns 422 "pull request already exists"
    when ``branch_name`` already has one open -- e.g. a second Deliver click
    re-committing to the same branch before the first PR merged/closed.
    Every PR-opening function in this module used to fall back to
    constructing an inert ``{repo_url}/compare/{branch_name}`` link for this
    case -- clickable, but never resolvable to a real lifecycle:
    ``get_pr_status()`` can look one up by head branch (the same query this
    function makes), but that only ever runs on a *later* page load, so the
    very PR history/Ledger row this call's own return value seeds starts
    out permanently stuck on "Unknown" until then, and every fresh delivery
    to an already-open PR (e.g. a rejection-review edit + re-deliver) kept
    re-showing that same dead-end link instead of the real, already-open
    PR a human could actually click through to.

    Looks up and returns that real PR's own ``html_url`` immediately instead,
    so this never happens: an already-open PR's URL comes back exactly the
    same whether this is the first commit or the fifth. Falls back to
    ``fallback_url`` (the same ``/compare/{branch_name}`` link, still a
    valid, clickable way to find the PR manually) only if the lookup itself
    fails -- never raises, since a URL-resolution problem here must not
    turn an otherwise-successful commit into an error.
    """
    try:
        resp = requests.get(
            f"{base_url}/pulls",
            headers=hdrs, timeout=10,
            params={"head": f"{owner}:{branch_name}", "state": "all", "per_page": 1},
        )
        resp.raise_for_status()
        prs = resp.json()
        if prs:
            return prs[0]["html_url"]
    except Exception:
        logger.warning("Failed to look up existing PR for %s:%s", owner, branch_name, exc_info=True)
    return fallback_url


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
                # Number of commits on the PR -- every AgentIT PR-opening
                # function (create_onboarding_pr/
                # create_source_patch_pr/commit_to_infra_repo) makes exactly
                # one commit before opening the PR, so >1 here means a human
                # pushed additional commits before it was merged/closed --
                # see get_pr_extra_commits(), the real pre-merge-edit signal
                # this backs (pr_outcomes.py).
                "commits": data.get("commits", 0),
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


def get_pr_extra_commits(pr_url: str, max_commits: int = 5) -> list[dict]:
    """Real commits pushed to ``pr_url`` AFTER AgentIT's own original commit
    -- the durable, factual signal that a human edited AgentIT's proposed
    content before merging/closing it (see docs on the removed ``gates``
    system's replacement: a merged PR's outcome must capture whether it
    landed exactly as proposed).

    Every AgentIT PR-opening function (``create_onboarding_pr``/
    ``create_source_patch_pr``/``commit_to_infra_repo``)
    makes exactly one commit before opening the PR -- so the first commit
    returned by ``GET /pulls/{n}/commits`` is always AgentIT's own, and
    anything after it was pushed by someone else. Returns each such commit
    as ``{"sha", "message", "author", "files": [{"filename", "additions",
    "deletions", "patch"}]}`` -- the real diff of that commit alone, fetched
    via ``GET /repos/{owner}/{repo}/commits/{sha}`` -- capped at
    ``max_commits`` to bound API usage. Returns ``[]`` on any failure, when
    there's only one commit (nothing to report), or when the PR URL can't
    be parsed -- callers must treat that as "no edit signal available,"
    never fabricate one.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        parts = pr_url.rstrip("/").split("/")
        if "/pull/" not in pr_url or len(parts) < 2 or parts[-2] != "pull":
            return []
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]
        resp = requests.get(
            f"{_API}/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            headers=hdrs, timeout=10, params={"per_page": 100},
        )
        resp.raise_for_status()
        commits = resp.json()
    except Exception:
        logger.warning("Failed to list commits for %s", pr_url, exc_info=True)
        return []

    if len(commits) <= 1:
        return []

    extra: list[dict] = []
    for commit in commits[1 : max_commits + 1]:
        sha = commit.get("sha", "")
        if not sha:
            continue
        commit_info = commit.get("commit", {})
        entry = {
            "sha": sha,
            "message": (commit_info.get("message") or "").split("\n", 1)[0],
            "author": commit_info.get("author", {}).get("name", ""),
            "files": [],
        }
        try:
            detail_resp = requests.get(
                f"{_API}/repos/{owner}/{repo}/commits/{sha}", headers=hdrs, timeout=10,
            )
            detail_resp.raise_for_status()
            detail = detail_resp.json()
            entry["files"] = [
                {
                    "filename": f.get("filename", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": f.get("patch", ""),
                }
                for f in (detail.get("files") or [])
            ]
        except Exception:
            logger.warning("Failed to fetch commit detail for %s@%s", pr_url, sha, exc_info=True)
        extra.append(entry)
    return extra


def close_pr(pr_url: str, reason: str = "") -> dict:
    """Close ``pr_url`` without merging -- the real, honest counterpart to
    ``merge_pr()`` above for the "this shouldn't ship" outcome. Posts
    ``reason`` (when given) as a real PR comment before closing, both for
    human visibility on GitHub itself and so a later
    ``fetch_pr_close_comments()``/``parse_reject_reason()`` pass (see
    ``capability_scout.py``, the pattern this reuses) can read the same
    reason back. Returns ``{"closed": True}`` or ``{"error": str}``.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        parts = pr_url.rstrip("/").split("/")
        if "/pull/" not in pr_url or len(parts) < 2 or parts[-2] != "pull":
            return {"error": f"not a PR URL: {pr_url}"}
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]

        if reason:
            requests.post(
                f"{_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                headers=hdrs, timeout=10, json={"body": reason},
            )

        resp = requests.patch(
            f"{_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=hdrs, timeout=15, json={"state": "closed"},
        )
        if resp.status_code >= 400:
            return {"error": f"GitHub API error: {resp.text[:200]}"}
        return {"closed": True}
    except Exception as exc:
        logger.exception("Failed to close PR %s", pr_url)
        return {"error": str(exc)}


def resolve_agentit_repo_url(cwd: Path | str | None = None) -> str:
    """Best-effort URL of AgentIT's own GitHub repo for REST PR helpers.

    Order: ``AGENTIT_REPO_URL``, ``GITHUB_REPOSITORY`` (``owner/name``),
    then ``git remote get-url origin`` in ``cwd`` (or process cwd). Falls
    back to the public AgentIT GitHub URL when nothing else resolves --
    callers that need a hard failure should check the returned host.
    """
    import subprocess

    explicit = (os.environ.get("AGENTIT_REPO_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/").removesuffix(".git")
    gh_repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if gh_repo and "/" in gh_repo:
        return f"https://github.com/{gh_repo}"
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
            cwd=str(cwd) if cwd else None,
        )
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip()
            if url.startswith("git@"):
                # git@github.com:owner/repo.git -> https://github.com/owner/repo
                path = url.split(":", 1)[-1].removesuffix(".git")
                host = url.split("@", 1)[-1].split(":", 1)[0]
                return f"https://{host}/{path}"
            return url.rstrip("/").removesuffix(".git")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "https://github.com/alimobrem/AgentIT"


def list_pull_requests(
    repo_url: str | None = None,
    *,
    state: str = "open",
    limit: int = 50,
    head_prefix: str | None = None,
    soft_fail: bool = True,
) -> list[dict]:
    """List PRs via ``GET /repos/{owner}/{repo}/pulls`` (no ``gh`` CLI).

    Returns ``[{"pr_url", "title", "headRefName", "state"}, ...]``. When
    ``head_prefix`` is set, only heads starting with that prefix are kept
    (capability-scout's ``agentit/self-improve/*`` filter).

    ``soft_fail=True`` (default): returns ``[]`` on any failure (outcome-
    sync discovery). ``soft_fail=False``: raises so fail-closed gates
    (open-PR cap) never confuse "API down" with "zero open PRs."
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        url = repo_url or resolve_agentit_repo_url()
        owner, repo = _parse_owner_repo(url)
        # GitHub only accepts open|closed|all for this endpoint.
        api_state = state if state in ("open", "closed", "all") else "all"
        collected: list[dict] = []
        page = 1
        per_page = min(100, max(1, limit))
        while len(collected) < limit:
            resp = requests.get(
                f"{_API}/repos/{owner}/{repo}/pulls",
                headers=hdrs, timeout=30,
                params={"state": api_state, "per_page": per_page, "page": page},
            )
            if resp.status_code >= 400:
                msg = f"list_pull_requests failed for {owner}/{repo}: {resp.text[:200]}"
                if soft_fail:
                    logger.warning("%s", msg)
                    return []
                raise RuntimeError(msg)
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for pr in batch:
                head = ((pr.get("head") or {}).get("ref")) or ""
                if head_prefix and not head.startswith(head_prefix):
                    continue
                collected.append({
                    "pr_url": pr.get("html_url") or "",
                    "title": pr.get("title") or "",
                    "headRefName": head,
                    "state": "merged" if pr.get("merged_at") else (pr.get("state") or "unknown"),
                })
                if len(collected) >= limit:
                    break
            if len(batch) < per_page:
                break
            page += 1
        return collected[:limit]
    except Exception as exc:
        if soft_fail:
            logger.warning("list_pull_requests unavailable: %s", exc)
            return []
        raise


def fetch_pr_issue_comments(pr_url: str) -> list[str]:
    """PR/issue comment bodies via REST (replaces ``gh pr view --json comments``).

    Uses ``GET /repos/{owner}/{repo}/issues/{n}/comments`` -- the same
    thread humans write close/reject reasons on. Returns ``[]`` on failure.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        parts = pr_url.rstrip("/").split("/")
        if "/pull/" not in pr_url or len(parts) < 2 or parts[-2] != "pull":
            return []
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]
        resp = requests.get(
            f"{_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=hdrs, timeout=30, params={"per_page": 100},
        )
        if resp.status_code >= 400:
            return []
        rows = resp.json()
        if not isinstance(rows, list):
            return []
        return [str(c.get("body") or "") for c in rows if isinstance(c, dict)]
    except Exception as exc:
        logger.warning("fetch_pr_issue_comments failed for %s: %s", pr_url, exc)
        return []


def open_draft_pull_request(
    repo_url: str,
    *,
    head: str,
    title: str,
    body: str,
    base: str = "main",
) -> dict:
    """Open a *draft* PR via ``POST /repos/{owner}/{repo}/pulls`` (no ``gh``).

    Returns ``{"pr_url": ...}`` or ``{"error": ...}``. If a PR for ``head``
    already exists (HTTP 422), resolves its real URL like
    ``_open_pr_with_fallback``.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"
        resp = requests.post(
            f"{base_url}/pulls",
            headers=hdrs, timeout=30,
            json={
                "title": title, "body": body, "head": head, "base": base,
                "draft": True,
            },
        )
        if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
            return {
                "pr_url": _find_existing_pr_url(
                    base_url, hdrs, owner, head, f"{repo_url.rstrip('/')}/compare/{head}",
                ),
            }
        if resp.status_code >= 400:
            return {"error": f"GitHub API error: {resp.text[:500]}"}
        return {"pr_url": resp.json().get("html_url") or ""}
    except Exception as exc:
        logger.warning("open_draft_pull_request failed: %s", exc)
        return {"error": str(exc)}


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


def get_commits_behind(repo_url: str, base_sha: str, head_ref: str = "main") -> dict:
    """How many commits (and how long) ``head_ref`` has moved ahead of
    ``base_sha``, via GitHub's Compare API. Used by ``DriftDetector``
    (watchers/drift_detector.py) to catch a stalled GitOps pipeline --
    commits landing on ``head_ref`` but never reaching what's actually
    deployed (the concrete gap in the 2026-07-17 incident: notify-argocd
    stuck on pod scheduling/etcd pressure for hours with no signal that
    main had stopped reaching the cluster).

    Unlike every other function in this module, a ``GITHUB_TOKEN`` is
    optional here: GitHub's compare endpoint works unauthenticated for
    public repos, so a missing token never blocks this one check. Returns
    ``{}`` (never a fabricated value) on any failure -- unreachable API,
    unknown SHA, rate limit, etc. Callers must treat that as "lag unknown
    this tick", never "in sync".
    """
    try:
        owner, repo = _parse_owner_repo(repo_url)
        token = os.environ.get("GITHUB_TOKEN", "")
        hdrs = _headers(token) if token else {"Accept": "application/vnd.github+json"}
        resp = requests.get(
            f"{_API}/repos/{owner}/{repo}/compare/{base_sha}...{head_ref}",
            headers=hdrs, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("get_commits_behind(%s, %s..%s) failed: %s", repo_url, base_sha, head_ref, exc)
        return {}

    ahead_by = data.get("ahead_by", 0)
    hours_behind = None
    commits = data.get("commits") or []
    if ahead_by > 0 and commits:
        oldest_date = commits[0].get("commit", {}).get("committer", {}).get("date", "")
        if oldest_date:
            try:
                oldest = datetime.fromisoformat(oldest_date.replace("Z", "+00:00"))
                hours_behind = (datetime.now(timezone.utc) - oldest).total_seconds() / 3600.0
            except Exception:
                logger.debug("Could not parse commit date %r", oldest_date)
    return {
        "ahead_by": ahead_by,
        "behind_by": data.get("behind_by", 0),
        "status": data.get("status", "unknown"),
        "hours_behind": hours_behind,
    }


# ── Shared PR-opening primitives ────────────────────────────────────────
#
# create_onboarding_pr(), create_source_patch_pr(), and
# commit_to_infra_repo() share the same 8-step GitHub sequence (fetch
# default branch -> base SHA -> tree -> commit -> ref -> PR). Per-Agent
# PRs (`create_agent_prs`) were removed as a product path; Scan/
# auto_delivery remains the sole GitOps/chart PR creator.


def _get_default_branch_and_base_sha(base_url: str, hdrs: dict) -> tuple[str, str]:
    """Fetch a repo's default branch and its current head SHA -- the first
    two GitHub API calls every PR-opening function below makes before
    building a tree. Raises (``requests.HTTPError`` or otherwise) exactly
    like the inlined calls this replaces did; each caller's own
    try/except handles it identically to before."""
    resp = requests.get(base_url, headers=hdrs, timeout=10)
    resp.raise_for_status()
    default_branch = resp.json()["default_branch"]

    resp = requests.get(
        f"{base_url}/git/ref/heads/{default_branch}",
        headers=hdrs, timeout=10,
    )
    resp.raise_for_status()
    base_sha = resp.json()["object"]["sha"]
    return default_branch, base_sha


def _commit_tree(base_url: str, hdrs: dict, base_sha: str, tree_items: list[dict], message: str) -> str:
    """Build a tree from ``tree_items`` on top of ``base_sha``, then a
    commit with ``message`` on top of that tree -- the middle two GitHub
    API calls every PR-opening function below makes. Returns the new
    commit's SHA."""
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
        json={"message": message, "tree": tree_sha, "parents": [base_sha]},
    )
    resp.raise_for_status()
    return resp.json()["sha"]


def _create_or_update_branch_ref(base_url: str, hdrs: dict, branch_name: str, commit_sha: str) -> None:
    """Point ``branch_name`` at ``commit_sha`` -- create the ref if it
    doesn't exist yet, force-update (force-push) it if it does (a 422
    means the ref already exists). The identical create-or-force-push
    fallback every PR-opening function below already repeated."""
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


def _open_pr_with_fallback(
    base_url: str, hdrs: dict, owner: str, branch_name: str, base_branch: str,
    title: str, body: str, repo_url: str,
) -> str:
    """Open a PR from ``branch_name`` into ``base_branch`` -- if one
    already exists for this branch (HTTP 422, "pull request already
    exists"), resolve its real URL via ``_find_existing_pr_url()`` instead
    of treating that as a failure. Returns the PR's ``html_url`` either
    way. The identical open-or-find-existing fallback every PR-opening
    function below already repeated."""
    resp = requests.post(
        f"{base_url}/pulls",
        headers=hdrs, timeout=10,
        json={"title": title, "body": body, "head": branch_name, "base": base_branch},
    )
    if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
        return _find_existing_pr_url(base_url, hdrs, owner, branch_name, f"{repo_url}/compare/{branch_name}")
    resp.raise_for_status()
    return resp.json()["html_url"]


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

        default_branch, base_sha = _get_default_branch_and_base_sha(base_url, hdrs)

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

        commit_sha = _commit_tree(
            base_url, hdrs, base_sha, tree_items,
            "feat: add AgentIT enterprise onboarding manifests\n\nGenerated by AgentIT Enterprise Readiness Platform",
        )
        _create_or_update_branch_ref(base_url, hdrs, branch_name, commit_sha)

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

        pr_url = _open_pr_with_fallback(
            base_url, hdrs, owner, branch_name, default_branch,
            "AgentIT Enterprise Onboarding", pr_body, repo_url,
        )
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


def path_exists_on_default_branch(repo_url: str, path: str) -> bool | None:
    """Whether ``path`` exists on the repo's default branch.

    Returns ``True`` (exists), ``False`` (404 / absent), or ``None`` when
    the lookup cannot be completed (missing token, network/API error). The
    self-managed chart delivery gate treats ``None`` as fail-closed refuse.
    """
    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"
        default_branch, _ = _get_default_branch_and_base_sha(base_url, hdrs)
        resp = requests.get(
            f"{base_url}/contents/{path}",
            headers=hdrs, timeout=10, params={"ref": default_branch},
        )
    except Exception:
        logger.warning("path_exists_on_default_branch lookup failed for %s", path, exc_info=True)
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    logger.warning(
        "path_exists_on_default_branch unexpected status %s for %s",
        resp.status_code, path,
    )
    return None


def _infra_repo_content_unchanged(
    base_url: str, hdrs: dict, app_name: str, files: list[dict], default_branch: str,
) -> bool:
    """True only if every one of ``files`` is byte-identical to what's
    already committed at its destination path
    (``apps/{app_name}/{category}/{filename}``) on the freshly-fetched
    ``default_branch``.

    ``commit_to_infra_repo()`` is the primary onboarding delivery mechanism
    (every GitOps-registered app's cluster-config/CI-CD-shared-namespace
    manifests route through it via ``delivery.py``'s
    ``_deliver_via_gitops_pr()``) and, unlike ``create_agent_prs()`` (which
    got the analogous dedup check for the "recurring redundant-PR pattern",
    PRs #85/#89/#90/#91), never had this guard. That was a latent gap
    while onboarding only ever ran from an explicit human click; the
    2026-07-20 unify-scan-onboard-chain work makes every Assess/Scan
    (including cadence/webhook-triggered re-assessments of an app that's
    already onboarded and unchanged) automatically chain into onboarding
    every time, which would otherwise branch/commit/force-push/open-a-PR
    on every single tick even when nothing changed.
    """
    for f in files:
        category = f.get("category", "misc")
        filename = Path(f["path"]).name
        target_path = f"apps/{app_name}/{category}/{filename}"
        existing = _get_file_content_at_ref(base_url, hdrs, target_path, default_branch)
        if existing != f["content"]:
            return False
    return True


def merge_pr(pr_url: str) -> dict:
    """Merge a GitHub PR via the REST API.

    Used only by the real, direct Merge action on a still-open PR
    (``routes/pr_actions.py::merge_pr_route``): a human clicking Merge PR
    *is* the merge action -- AgentIT itself never calls this to auto-merge
    on its own initiative, matching the design doc's explicit "a human
    always merges into a self-healing/pruning GitOps repo" posture (see
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
    pr_context: dict | None = None,
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

    Belt-and-suspenders: any ``chart/``-targeted file must pass
    ``delivery.validate_self_managed_chart_delivery`` (Helm-shaped, no
    forbidden kinds, no collision on default branch) before a PR opens —
    ``route_and_deliver`` already gates this; this refuses direct callers.
    """
    chart_files = [
        f for f in files
        if str(f.get("target_path") or f.get("path") or "").startswith("chart/")
    ]
    if chart_files:
        from agentit.portal.delivery import validate_self_managed_chart_delivery

        path_exists: dict[str, bool | None] = {}
        for f in chart_files:
            target = f.get("target_path") or f["path"]
            path_exists[target] = path_exists_on_default_branch(repo_url, target)
        gate_reason = validate_self_managed_chart_delivery(
            chart_files, path_exists=path_exists,
        )
        if gate_reason:
            return {"error": gate_reason, "gate_refused": True}

    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        default_branch, base_sha = _get_default_branch_and_base_sha(base_url, hdrs)

        tree_items = []
        for f in files:
            target = f.get("target_path") or f["path"]
            tree_items.append({
                "path": target,
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            })

        commit_sha = _commit_tree(
            base_url, hdrs, base_sha, tree_items,
            f"fix(agentit): {len(files)} source-level change(s) for {repo_name}",
        )
        _create_or_update_branch_ref(base_url, hdrs, branch_name, commit_sha)

        if pr_context and pr_context.get("body"):
            pr_body = str(pr_context["body"])
            # Distinguish Scan chart/cluster source PRs from true codechange
            # patches by mechanism label in the title (quality-review P2).
            cluster = pr_context.get("cluster_key")
            if cluster:
                pr_title = f"[AgentIT] Scan {cluster}: source-repo patch for {repo_name}"
            else:
                pr_title = f"[AgentIT] source-repo patch for {repo_name}"
        else:
            file_list = "\n".join(
                f"- `{f.get('target_path') or f['path']}` — {f.get('description', '')}"
                for f in files
            )
            pr_body = (
                "## AgentIT: source-repo patch\n\n"
                f"Real patch(es) against {len(files)} file(s) in this repo "
                "(not a loose `.agentit/` copy; not a chart Scan dump).\n\n"
                f"### Files\n{file_list}\n\n"
                "Argo deploys after merge; AgentIT does **not** auto-merge.\n\n"
                "> Generated by [AgentIT](https://github.com/alimobrem/AgentIT)"
            )
            pr_title = f"[AgentIT] source-repo patch ({len(files)} file(s)) for {repo_name}"

        pr_url = _open_pr_with_fallback(
            base_url, hdrs, owner, branch_name, default_branch,
            pr_title, pr_body, repo_url,
        )
        return {"pr_url": pr_url, "branch": branch_name, "files_committed": len(files)}

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
    pr_context: dict | None = None,
) -> dict:
    """Commit onboarding manifests to the GitOps infra repo.

    Files are placed under apps/{app_name}/{category}/{filename}.
    Creates a branch and PR if branch_name is set, otherwise commits to main.

    Returns {"commit_url", "pr_url", "files_committed"} or {"error"}.
    Refuses ``apps/agentit/`` outright (AppSet excludes that path; Application
    ``agentit`` syncs Helm ``chart/`` from AgentIT.git — see
    docs/architecture-agentit-vs-fleet-gitops.md). Also refuses an empty
    ``files`` list so we never open a zero-file PR.
    """
    app_name = app_name.lower().replace("_", "-").replace(".", "-")
    branch_name = branch_name or f"agentit/{app_name}"

    if app_name == "agentit":
        return {
            "error": (
                "refusing to commit under apps/agentit/ — Application `agentit` syncs "
                "Helm chart/ from AgentIT.git, and ApplicationSet excludes apps/agentit "
                "(dead letter). Route self-managed AgentIT via route_and_deliver() to "
                "AgentIT.git instead — see docs/architecture-agentit-vs-fleet-gitops.md"
            ),
        }
    if not files:
        return {"skipped": True, "reason": "no files to commit -- refusing empty PR"}

    try:
        token = _get_token()
        hdrs = _headers(token)
        owner, repo = _parse_owner_repo(infra_repo_url)
        base_url = f"{_API}/repos/{owner}/{repo}"

        default_branch, base_sha = _get_default_branch_and_base_sha(base_url, hdrs)

        if _infra_repo_content_unchanged(base_url, hdrs, app_name, files, default_branch):
            logger.info(
                "agentit: onboarding manifests for %s unchanged from %s -- skipping commit/PR",
                app_name, default_branch,
            )
            return {
                "skipped": True,
                "reason": f"content already matches {default_branch} -- no PR needed",
            }

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

        commit_sha = _commit_tree(
            base_url, hdrs, base_sha, tree_items,
            f"feat(agentit): onboard {app_name} — {len(files)} manifests",
        )
        _create_or_update_branch_ref(base_url, hdrs, branch_name, commit_sha)

        if pr_context and pr_context.get("body"):
            pr_body = str(pr_context["body"])
            pr_title = (
                f"[AgentIT] {pr_context.get('cluster_key') or 'Scan'} for {app_name}"
            )
        else:
            file_list = "\n".join(
                f"- `apps/{app_name}/{f.get('category', 'misc')}/{Path(f['path']).name}`"
                for f in files
            )
            pr_body = (
                f"## AgentIT: onboard {app_name}\n\n"
                f"Manifests committed to the GitOps infra repo under `apps/{app_name}/`.\n\n"
                f"### Files\n{file_list}\n\n"
                "Argo deploys after merge; AgentIT does **not** auto-merge.\n\n"
                f"> Generated by [AgentIT](https://github.com/alimobrem/AgentIT)"
            )
            pr_title = f"[AgentIT] Onboard {app_name}"

        pr_url = _open_pr_with_fallback(
            base_url, hdrs, owner, branch_name, default_branch,
            pr_title, pr_body, infra_repo_url,
        )

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


MANAGED_APPS_APPLICATIONSET_NAME = "agentit-managed-apps"
MANAGED_APPS_APPLICATIONSET_NAMESPACE = "openshift-gitops"

# AgentIT's own repo -- the same default `cli.py`'s `self-assess`/`self-fix`
# commands and `routes/assessments.py`'s `/self-assess` route already use.
# Reused here purely as the owner-resolution seed for
# `expected_managed_apps_repo_url()` below: this fleet is single-tenant (one
# GitHub owner backs every onboarded app's shared `agentit-gitops` infra
# repo), so AgentIT's own owner is that owner -- not a new convention.
_AGENTIT_SELF_REPO_URL = "https://github.com/alimobrem/AgentIT"


def expected_managed_apps_repo_url() -> str:
    """The git source repoURL the fleet-wide ``agentit-managed-apps``
    ApplicationSet (built by ``ensure_applicationset()`` below) should
    always have.

    Derived the exact same way ``_auto_create_infra_repo()``
    (``routes/assessments.py``) computes it when it calls
    ``ensure_infra_repo()``: resolve the owner via ``_parse_owner_repo()``
    (this module's one owner-resolution routine, used by every other call
    site here) and apply ``ensure_infra_repo()``'s own "one shared
    ``agentit-gitops`` repo per GitHub owner" naming convention -- never a
    second, independently hardcoded guess of the final URL.

    Used by ``DriftDetector`` (``watchers/drift_detector.py``) to detect and
    self-heal the 2026-07-18 incident: something entirely outside this
    repo's code ran ``oc create``/``oc patch`` directly against the live
    cluster and overwrote this ApplicationSet's repoURL with a bogus
    placeholder -- twice in one day -- breaking GitOps rollout for the
    entire fleet until a human noticed and manually restored it each time.
    """
    owner, _ = _parse_owner_repo(_AGENTIT_SELF_REPO_URL)
    return f"https://github.com/{owner}/agentit-gitops"


def ensure_applicationset(infra_repo_url: str) -> bool:
    """Ensure an Argo CD ApplicationSet exists for the infra repo."""
    if not is_trusted_git_host(infra_repo_url):
        logger.warning(
            "Skipping ApplicationSet: infra_repo_url host not in trusted domains %s: %s",
            _TRUSTED_GIT_DOMAINS, infra_repo_url,
        )
        return False

    name = MANAGED_APPS_APPLICATIONSET_NAME
    namespace = MANAGED_APPS_APPLICATIONSET_NAMESPACE
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
                    # recurse + yaml-only: fleet apps land manifests under
                    # apps/{app}/{category}/ and apps/{app}/skills/ — without
                    # recurse Argo Directory mode sees zero top-level YAML
                    # (Synced/Healthy with 0 resources; live HPA/quota never
                    # update). include excludes grafana *.json / *.md / *.sh
                    # that otherwise fail manifest unmarshal.
                    "source": {
                        "repoURL": infra_repo_url,
                        "targetRevision": "HEAD",
                        "path": "{{path}}",
                        "directory": {
                            "recurse": True,
                            "include": "{*.yaml,*.yml}",
                        },
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

        # Verify TLS by default (secure default for clusters with a real,
        # publicly-trusted cert). Self-signed-ingress dev clusters (e.g. the
        # default OpenShift wildcard cert) make GitHub's webhook delivery
        # fail every single time with "certificate signed by unknown
        # authority" -- confirmed live via `gh api repos/.../hooks/{id}/
        # deliveries` showing 100% failures for this exact reason, which
        # silently starves `check_pending_delivery_verifications()` of the
        # push events it needs, leaving deliveries stuck showing "Awaiting
        # verification" forever. `AGENTIT_WEBHOOK_INSECURE_SSL=1` opts a
        # cluster into skipping verification, mirroring the CI webhook's
        # already-hand-patched `insecure_ssl` (see docs/deployment.md).
        insecure_ssl = "1" if os.environ.get("AGENTIT_WEBHOOK_INSECURE_SSL", "").strip().lower() in (
            "1", "true", "yes",
        ) else "0"
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
                    "insecure_ssl": insecure_ssl,
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


def check_webhook_delivery_health(repo_url: str, url_suffix: str = "/api/webhook/github-push") -> dict:
    """Real liveness check for a managed repo's registered push webhook --
    "is GitHub actually delivering push events to us", not just "is a hook
    registered". Used by the Health page's Webhook Deliveries section.

    Registration alone (``ensure_webhook()`` above) proved nothing about
    whether GitHub could actually reach the app -- the 2026-07-18 "Awaiting
    verification" incident had a webhook registered, active, and 100%
    failing (oauth-proxy's ``--skip-auth-regex`` 302'ing every delivery to
    the OAuth login page, plus a second hook independently failing TLS
    verification), and nothing surfaced that short of a human manually
    running `gh api repos/.../hooks/{id}/deliveries`. This does exactly
    that check, automatically: ``GET .../hooks`` to find the registered
    hook (matched by URL suffix, not the full URL, so this doesn't need to
    reconstruct this app's own external base URL), then ``GET .../hooks/
    {id}/deliveries`` for its most recent delivery outcome.

    Returns ``{"ok": bool | None, "status": str, "detail": str}``.
    ``ok=None`` ("no_deliveries") is a deliberately distinct, non-failing
    "inconclusive" state (a brand new hook GitHub hasn't called yet) --
    different from ``ok=False``'s "registered but actually failing".
    Never raises: every GitHub API call is wrapped, matching
    ``check_github_token()``'s convention above.
    """
    try:
        token = _get_token()
    except RuntimeError:
        return {
            "ok": False, "status": "no_token",
            "detail": "GITHUB_TOKEN is not set -- cannot check webhook delivery health",
        }
    hdrs = _headers(token)

    try:
        owner, repo = _parse_owner_repo(repo_url)
        hooks_resp = requests.get(f"{_API}/repos/{owner}/{repo}/hooks", headers=hdrs, timeout=10)
        hooks_resp.raise_for_status()
        hook = next(
            (h for h in hooks_resp.json() if h.get("config", {}).get("url", "").endswith(url_suffix)),
            None,
        )
    except Exception as exc:
        logger.warning("Failed to list webhooks on %s: %s", repo_url, exc)
        return {"ok": False, "status": "error", "detail": f"Could not list webhooks: {exc}"}

    if hook is None:
        return {
            "ok": False, "status": "not_registered",
            "detail": f"No webhook ending in {url_suffix} is registered on this repo",
        }
    if not hook.get("active", True):
        return {
            "ok": False, "status": "inactive",
            "detail": f"Webhook {hook['id']} is registered but disabled on GitHub",
        }

    try:
        deliveries_resp = requests.get(
            f"{_API}/repos/{owner}/{repo}/hooks/{hook['id']}/deliveries",
            headers=hdrs, params={"per_page": 5}, timeout=10,
        )
        deliveries_resp.raise_for_status()
        deliveries = deliveries_resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch deliveries for webhook %s on %s: %s", hook["id"], repo_url, exc)
        return {"ok": False, "status": "error", "detail": f"Could not fetch delivery history: {exc}"}

    if not deliveries:
        return {
            "ok": None, "status": "no_deliveries",
            "detail": f"Webhook {hook['id']} registered but has no recorded deliveries yet",
        }

    latest = deliveries[0]
    status_code = latest.get("status_code")
    delivered_at = latest.get("delivered_at", "?")
    if status_code is not None and 200 <= status_code < 300:
        return {
            "ok": True, "status": "delivering",
            "detail": f"Last delivery at {delivered_at}: HTTP {status_code} ({latest.get('status', 'OK')})",
        }

    # Gateway / timeout codes during canary or pod rollout (verified live
    # 2026-07-21: tip promote of 7347003 produced a single 503 while earlier
    # deliveries in the same hour were 200). Pinning Self-Health Critical
    # with oauth-proxy remediation advice for that blip is wrong -- treat as
    # inconclusive when recent history shows the hook was delivering.
    _GATEWAY_BLIP_CODES = {502, 503, 504}
    sample = deliveries[:5]
    recent_ok = sum(
        1 for d in sample
        if isinstance(d.get("status_code"), int) and 200 <= d["status_code"] < 300
    )
    if status_code in _GATEWAY_BLIP_CODES and recent_ok >= 2:
        return {
            "ok": None, "status": "transient",
            "detail": (
                f"Last delivery at {delivered_at}: HTTP {status_code} "
                f"({latest.get('status', 'unknown')}) -- but {recent_ok} of the "
                f"last {len(sample)} deliveries succeeded. Likely a brief outage "
                "during a canary/rollout; clears on the next successful push "
                "(or a GitHub webhook ping)."
            ),
        }

    if status_code in (301, 302, 303, 307, 308):
        hint = (
            "GitHub hit an HTTP redirect instead of the app -- check "
            "oauth-proxy's --skip-auth-regex covers ^/api/webhook/ "
            "(chart/templates/deployment.yaml)"
        )
    elif status_code in _GATEWAY_BLIP_CODES:
        hint = (
            "GitHub reached the Route but got a gateway/timeout error -- "
            "check portal pods are Ready (canary/rollout) before blaming "
            "oauth-proxy or insecure_ssl"
        )
    elif status_code == 0:
        hint = (
            "GitHub could not complete TLS to this app -- check the hook's "
            "insecure_ssl setting for self-signed ingress certs "
            "(docs/deployment.md webhook section)"
        )
    else:
        hint = (
            "GitHub is not reaching this app; check oauth-proxy's "
            "--skip-auth-regex and the hook's insecure_ssl setting"
        )
    return {
        "ok": False, "status": "failing",
        "detail": (
            f"Last delivery at {delivered_at}: HTTP {status_code} "
            f"({latest.get('status', 'unknown')}) -- {hint}"
        ),
    }
