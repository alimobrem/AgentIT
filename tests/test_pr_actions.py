"""routes/pr_actions.py -- Merge/Close, the real, direct actions that
replaced the gitops-pr-pending/-shared-namespace gates (and, in effect,
every other delivery category's approval step too, since the gates table's
generic resolve/create machinery has been removed entirely, 2026-07-19)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from agentit.portal.delivery import MECHANISM_INFRA_REPO_COMMIT
from conftest import make_report, make_store, prime_csrf


@pytest.fixture
async def pr_actions_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.pr_actions.get_store", return_value=store):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True,
        ) as client:
            await prime_csrf(client)
            yield client, store


async def _seed_open_pr(store, *, app_name="pr-actions-app", pr_url="https://github.com/org/infra-gitops/pull/9"):
    aid = await store.save(make_report(repo_name=app_name))
    await store.create_delivery(
        aid, app_name, {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
        details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
    )
    return aid


class TestMergePr:
    async def test_merge_succeeds_and_logs_event(self, pr_actions_client):
        client, store = pr_actions_client
        aid = await _seed_open_pr(store)

        with patch("agentit.portal.github_pr.merge_pr") as mock_merge:
            mock_merge.return_value = {"merged": True, "sha": "abc123"}
            resp = await client.post(
                "/prs/merge",
                data={"pr_url": "https://github.com/org/infra-gitops/pull/9", "assessment_id": aid},
                follow_redirects=False,
            )

        mock_merge.assert_called_once_with("https://github.com/org/infra-gitops/pull/9")
        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]
        assert f"/assessments/{aid}?tab=ledger" in resp.headers["location"]
        events = await store.list_events()
        assert any(e["action"] == "gitops-pr-merged" for e in events)

    async def test_merge_failure_shows_error_and_does_not_log_merged_event(self, pr_actions_client):
        client, store = pr_actions_client
        aid = await _seed_open_pr(store)

        with patch("agentit.portal.github_pr.merge_pr") as mock_merge:
            mock_merge.return_value = {"error": "merge conflict"}
            resp = await client.post(
                "/prs/merge",
                data={"pr_url": "https://github.com/org/infra-gitops/pull/9", "assessment_id": aid},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        events = await store.list_events()
        assert not any(e["action"] == "gitops-pr-merged" for e in events)

    async def test_missing_pr_url_is_a_bad_request(self, pr_actions_client):
        client, _store = pr_actions_client
        resp = await client.post("/prs/merge", data={}, follow_redirects=False)
        assert resp.status_code == 400

    async def test_merge_with_no_assessment_id_redirects_to_ledger(self, pr_actions_client):
        client, store = pr_actions_client
        await _seed_open_pr(store)

        with patch("agentit.portal.github_pr.merge_pr") as mock_merge:
            mock_merge.return_value = {"merged": True, "sha": "abc123"}
            resp = await client.post(
                "/prs/merge",
                data={"pr_url": "https://github.com/org/infra-gitops/pull/9"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/ledger")


class TestClosePr:
    async def test_close_succeeds_and_records_outcome_immediately(self, pr_actions_client):
        client, store = pr_actions_client
        aid = await _seed_open_pr(store)

        with patch("agentit.portal.github_pr.close_pr") as mock_close:
            mock_close.return_value = {"closed": True}
            resp = await client.post(
                "/prs/close",
                data={
                    "pr_url": "https://github.com/org/infra-gitops/pull/9",
                    "reason": "manifest regressed a required probe",
                    "assessment_id": aid,
                    "app_name": "pr-actions-app",
                    "category": "cluster_config",
                },
                follow_redirects=False,
            )

        mock_close.assert_called_once_with(
            "https://github.com/org/infra-gitops/pull/9", "manifest regressed a required probe",
        )
        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]

        outcome = await store.get_pr_outcome("https://github.com/org/infra-gitops/pull/9")
        assert outcome is not None
        assert outcome["outcome"] == "rejected"
        assert outcome["reject_reason"] == "manifest regressed a required probe"
        assert outcome["app_name"] == "pr-actions-app"

        events = await store.list_events()
        assert any(e["action"] == "gitops-pr-closed" for e in events)

    async def test_close_failure_shows_error_and_does_not_record_outcome(self, pr_actions_client):
        client, store = pr_actions_client
        aid = await _seed_open_pr(store)

        with patch("agentit.portal.github_pr.close_pr") as mock_close:
            mock_close.return_value = {"error": "not found"}
            resp = await client.post(
                "/prs/close",
                data={
                    "pr_url": "https://github.com/org/infra-gitops/pull/9",
                    "reason": "wontfix",
                    "assessment_id": aid,
                    "app_name": "pr-actions-app",
                },
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert await store.get_pr_outcome("https://github.com/org/infra-gitops/pull/9") is None

    async def test_missing_pr_url_is_a_bad_request(self, pr_actions_client):
        client, _store = pr_actions_client
        resp = await client.post("/prs/close", data={}, follow_redirects=False)
        assert resp.status_code == 400
