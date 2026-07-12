"""Dependency lifecycle management -- process update PRs automatically."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DependencyUpdate:
    name: str
    old_version: str
    new_version: str
    update_type: str
    pr_url: str
    auto_mergeable: bool = False
    risk_level: str = "unknown"


def classify_update(name: str, old_version: str, new_version: str) -> tuple[str, str]:
    old_parts = old_version.lstrip("v^~>=<").split(".")
    new_parts = new_version.lstrip("v^~>=<").split(".")
    try:
        if old_parts[0] != new_parts[0]:
            return "major", "high"
        elif len(old_parts) > 1 and len(new_parts) > 1 and old_parts[1] != new_parts[1]:
            return "minor", "medium"
        else:
            return "patch", "low"
    except (IndexError, ValueError):
        return "unknown", "medium"


_SAFE_AUTO_MERGE = {"patch": True, "minor": False, "major": False}


def evaluate_pr(pr_title: str, pr_body: str, pr_url: str) -> DependencyUpdate | None:
    patterns = [
        r"(?:Updates?|Bumps?)\s+(?:dependency\s+)?(\S+)\s+from\s+v?(\S+)\s+to\s+v?(\S+)",
        r"(?:Updates?|Bumps?)\s+(?:dependency\s+)?(\S+)\s+to\s+v?(\S+)",
        r"chore\(deps\):\s+update\s+(\S+)\s+to\s+v?(\S+)",
    ]
    name, old_ver, new_ver = "", "", ""
    for text in (pr_title, pr_body):
        if not text:
            continue
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                groups = m.groups()
                if len(groups) == 3:
                    name, old_ver, new_ver = groups
                elif len(groups) == 2:
                    name, new_ver = groups
                    old_ver = "unknown"
                break
        if name and old_ver != "unknown":
            break
    if not name:
        return None
    update_type, risk = classify_update(name, old_ver, new_ver)
    return DependencyUpdate(
        name=name, old_version=old_ver, new_version=new_ver,
        update_type=update_type, pr_url=pr_url,
        auto_mergeable=_SAFE_AUTO_MERGE.get(update_type, False),
        risk_level=risk,
    )


def process_dependency_prs(repo_url: str, token: str | None = None) -> list[DependencyUpdate]:
    token = token or os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning("No GITHUB_TOKEN -- cannot process dependency PRs")
        return []

    import httpx

    parts = repo_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1].replace(".git", "")

    updates: list[DependencyUpdate] = []
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 50},
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Failed to list PRs: %s", resp.status_code)
            return []
        for pr in resp.json():
            user = pr.get("user", {}).get("login", "")
            if user not in ("renovate[bot]", "dependabot[bot]", "renovate", "dependabot"):
                continue
            update = evaluate_pr(pr.get("title", ""), pr.get("body", ""), pr.get("html_url", ""))
            if update:
                updates.append(update)
    except Exception as exc:
        logger.warning("Failed to process dependency PRs: %s", exc)

    return updates
