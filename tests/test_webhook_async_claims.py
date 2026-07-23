"""Async GitHub push (202) + durable incomplete webhook claims."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from conftest import make_report, make_store


@pytest.mark.asyncio
async def test_claim_incomplete_until_complete():
    store = await make_store()
    assert await store.claim_webhook("async-claim-1") is True
    assert await store.claim_webhook("async-claim-1") is False
    await store.complete_webhook_claim("async-claim-1")
    # Completed claims are permanent dedup — no reclaim.
    assert await store.claim_webhook("async-claim-1") is False


@pytest.mark.asyncio
async def test_stale_incomplete_claim_can_be_reclaimed():
    store = await make_store()
    assert await store.claim_webhook("stale-claim-1", stale_after_seconds=60) is True
    # Force the claim into the past so TTL reclaim fires.
    await store._pool.execute(
        "UPDATE processed_webhooks SET processed_at = $2 WHERE delivery_id = $1",
        "stale-claim-1",
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - timedelta(seconds=120),
    )
    assert await store.claim_webhook("stale-claim-1", stale_after_seconds=60) is True


@pytest.mark.asyncio
async def test_github_push_returns_202_and_completes_claim(portal_client):
    client, store, aid = portal_client
    report = await store.get(aid)
    repo_url = report.repo_url
    new_report = make_report(repo_name=report.repo_name, repo_url=repo_url)

    with patch(
        "agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report,
    ), patch(
        "agentit.portal.services.onboard_pipeline._run_onboarding_job",
        return_value=None,
    ):
        resp = await client.post(
            "/api/webhook/github-push",
            json={
                "ref": "refs/heads/main",
                "after": "deadbeefcafe",
                "pusher": {"name": "tester"},
                "commits": [],
                "repository": {"html_url": repo_url, "default_branch": "main"},
            },
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "async-202-delivery-1",
            },
        )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    # Background finished → completed claim blocks reclaim.
    assert await store.claim_webhook("async-202-delivery-1") is False


@pytest.mark.asyncio
async def test_github_push_hard_failure_releases_claim(portal_client, monkeypatch):
    client, store, aid = portal_client
    report = await store.get(aid)
    repo_url = report.repo_url

    with patch(
        "agentit.portal.routes.webhooks.clone_assess_cleanup",
        side_effect=RuntimeError("clone exploded"),
    ):
        resp = await client.post(
            "/api/webhook/github-push",
            json={
                "ref": "refs/heads/main",
                "after": "badbadbadbad",
                "pusher": {"name": "tester"},
                "commits": [],
                "repository": {"html_url": repo_url, "default_branch": "main"},
            },
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "async-fail-delivery-1",
            },
        )

    assert resp.status_code == 202
    assert await store.claim_webhook("async-fail-delivery-1") is True
