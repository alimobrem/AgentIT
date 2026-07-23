"""Portal clone+assess concurrency slot — OOM guard for concurrent webhooks.

Dogfood (pinky): two overlapping GitHub push assesses OOMKilled the portal at
512Mi mid-reassess → webhook 504 → push-driven finding verification never ran.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agentit.portal.helpers import (
    AssessBusyError,
    assess_concurrency_slot,
    clone_assess_cleanup,
)


@pytest.fixture(autouse=True)
def _reset_assess_slots(monkeypatch):
    """Each test gets a fresh default (max=1, acquire timeout=0.2s)."""
    monkeypatch.setenv("AGENTIT_ASSESS_MAX_CONCURRENT", "1")
    monkeypatch.setenv("AGENTIT_ASSESS_ACQUIRE_TIMEOUT", "0.2")
    import agentit.portal.helpers as helpers

    helpers._assess_slots_configured_for = -1
    yield
    helpers._assess_slots_configured_for = -1


def test_second_caller_raises_assess_busy_while_first_holds_slot():
    held = threading.Event()
    release = threading.Event()
    busy_seen = threading.Event()
    errors: list[BaseException] = []

    def holder():
        with assess_concurrency_slot():
            held.set()
            release.wait(timeout=5)

    def waiter():
        held.wait(timeout=5)
        try:
            with assess_concurrency_slot():
                pass
        except AssessBusyError:
            busy_seen.set()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    assert held.wait(timeout=2)
    assert busy_seen.wait(timeout=2), "second assess should fail soft while first holds the slot"
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert not errors


def test_clone_assess_cleanup_uses_concurrency_slot(monkeypatch):
    """The public webhook entrypoint must take the slot (not bypass it)."""
    entered = threading.Event()
    real_slot = assess_concurrency_slot

    def tracking_slot():
        entered.set()
        return real_slot()

    monkeypatch.setattr("agentit.portal.helpers.assess_concurrency_slot", tracking_slot)

    fake_report = MagicMock()
    with patch("agentit.cloner.clone_repo", return_value="/tmp/unused-assess"), \
         patch("agentit.runner.run_assessment", return_value=fake_report), \
         patch("agentit.portal.helpers.get_llm_client", return_value=None), \
         patch("shutil.rmtree"):
        assert clone_assess_cleanup("https://github.com/t/r", "medium") is fake_report
    assert entered.is_set()


@pytest.mark.asyncio
async def test_github_push_busy_releases_claim_for_retry(portal_client, monkeypatch):
    """202 + background busy exhaustion must release the claim for retry."""
    import asyncio

    from agentit.portal.helpers import AssessBusyError
    import agentit.portal.routes.webhooks as wh

    client, store, aid = portal_client
    report = await store.get(aid)
    repo_url = report.repo_url

    monkeypatch.setenv("AGENTIT_ASSESS_MAX_CONCURRENT", "1")
    monkeypatch.setenv("AGENTIT_ASSESS_ACQUIRE_TIMEOUT", "0.05")
    import agentit.portal.helpers as helpers

    helpers._assess_slots_configured_for = -1
    monkeypatch.setattr(wh, "_PUSH_BUSY_RETRIES", 1)
    monkeypatch.setattr(wh, "_PUSH_BUSY_SLEEP_SECONDS", 0)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with patch(
        "agentit.portal.routes.webhooks.clone_assess_cleanup",
        side_effect=AssessBusyError("another assessment is already in progress"),
    ):
        resp = await client.post(
            "/api/webhook/github-push",
            json={
                "ref": "refs/heads/main",
                "after": "abc123def456",
                "pusher": {"name": "tester"},
                "commits": [{"modified": ["README.md"], "added": []}],
                "repository": {
                    "html_url": repo_url,
                    "default_branch": "main",
                },
            },
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "busy-release-delivery-1",
            },
        )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    # Background task should have released the incomplete claim.
    assert await store.claim_webhook("busy-release-delivery-1") is True


@pytest.mark.asyncio
async def test_release_webhook_claim_allows_reclaim():
    from conftest import make_store

    store = await make_store()
    assert await store.claim_webhook("release-me") is True
    assert await store.claim_webhook("release-me") is False
    await store.release_webhook_claim("release-me")
    assert await store.claim_webhook("release-me") is True
