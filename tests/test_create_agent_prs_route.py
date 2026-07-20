"""Integration tests for POST /assessments/{id}/create-agent-prs ("Per-Agent
PRs") -- until 2026-07-20 this route bypassed portal/delivery.py's
secret-block/placeholder-block checks and GitOps-registration lookup
entirely, and never created a `deliveries` tracking row (only
`onboarding_results.pr_url`). See routes/assessments.py::create_agent_prs_route
for what's now applied and why it still doesn't route through
route_and_deliver() itself.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


def _skill_file(path: str = "app-network-policy.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


def _secret_file() -> dict:
    return {
        "category": "skills",
        "path": "db-secret.yaml",
        "content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: c2VjcmV0\n",
        "description": "should never be delivered",
    }


def _placeholder_cronjob_file() -> dict:
    return {
        "category": "cost",
        "path": "cost-cronjob.yaml",
        "content": (
            "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: cost\n"
            "spec:\n  jobTemplate:\n    spec:\n      template:\n        spec:\n"
            "          containers:\n          - name: job\n"
            "            image: REPLACE_WITH_AGENTIT_IMAGE\n"
        ),
        "description": "unresolved image placeholder",
    }


@pytest.fixture
async def agent_prs_client():
    store = await make_store()
    report = make_report(repo_name="test-app")
    assessment_id = await store.save(report)
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store, assessment_id


class TestCreateAgentPrsAppliesTheSameGuards:
    async def test_secret_file_is_never_delivered(self, agent_prs_client):
        """Regression: before this fix, Per-Agent PRs skipped
        classify_file()'s secret-block check entirely and would have
        committed a Secret manifest straight to the app's own repo."""
        client, store, aid = agent_prs_client
        await store.save_onboarding(aid, [_secret_file()])

        with patch("agentit.portal.github_pr.create_agent_prs") as mock_create:
            mock_create.return_value = []
            resp = await client.post(f"/assessments/{aid}/create-agent-prs", follow_redirects=False)

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        # The blocked-only path must never even reach github_pr.create_agent_prs
        # with the secret file -- confirm no agent batch was built for it.
        mock_create.assert_called_once()
        agent_results = mock_create.call_args[0][2]
        assert agent_results == []

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["details"]["blocked"] == ["db-secret.yaml"]

    async def test_placeholder_file_is_never_delivered(self, agent_prs_client):
        """Regression: before this fix, Per-Agent PRs skipped
        has_unresolved_placeholders() entirely and would have committed a
        manifest with a literal REPLACE_WITH_AGENTIT_IMAGE placeholder."""
        client, store, aid = agent_prs_client
        await store.save_onboarding(aid, [_placeholder_cronjob_file()])

        with patch("agentit.portal.github_pr.create_agent_prs") as mock_create:
            mock_create.return_value = []
            resp = await client.post(f"/assessments/{aid}/create-agent-prs", follow_redirects=False)

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        agent_results = mock_create.call_args[0][2]
        assert agent_results == []

        deliveries = await store.list_deliveries(aid)
        assert deliveries[0]["details"]["placeholder_blocked"] == ["cost-cronjob.yaml"]

    async def test_mixed_batch_only_delivers_the_clean_file(self, agent_prs_client):
        """A batch with both a blocked file and a clean one must still
        deliver the clean one -- blocking is per-file, not all-or-nothing
        for the whole request."""
        client, store, aid = agent_prs_client
        await store.save_onboarding(aid, [_secret_file(), _skill_file()])

        with patch("agentit.portal.github_pr.create_agent_prs") as mock_create:
            mock_create.return_value = [
                {"agent_name": "skills", "pr_url": "https://github.com/org/test-app/pull/1",
                 "branch": "agentit/skills", "files_count": 1},
            ]
            resp = await client.post(f"/assessments/{aid}/create-agent-prs", follow_redirects=False)

        assert resp.status_code == 303
        assert "agent_prs=" in resp.headers["location"]
        agent_results = mock_create.call_args[0][2]
        assert [a["agent_name"] for a in agent_results] == ["skills"]
        assert agent_results[0]["files"] == [_skill_file()]

        deliveries = await store.list_deliveries(aid)
        assert deliveries[0]["details"]["blocked"] == ["db-secret.yaml"]
        assert deliveries[0]["status"] == "delivered"

    async def test_records_a_deliveries_row_for_tracking(self, agent_prs_client):
        """Regression: before this fix, Per-Agent PRs never created a
        `deliveries` row at all -- only `onboarding_results.pr_url`."""
        client, store, aid = agent_prs_client
        await store.save_onboarding(aid, [_skill_file()])

        with patch("agentit.portal.github_pr.create_agent_prs") as mock_create:
            mock_create.return_value = [
                {"agent_name": "skills", "pr_url": "https://github.com/org/test-app/pull/2",
                 "branch": "agentit/skills", "files_count": 1},
            ]
            resp = await client.post(f"/assessments/{aid}/create-agent-prs", follow_redirects=False)

        assert resp.status_code == 303
        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "delivered"
        assert deliveries[0]["details"]["outcomes"]["skills"]["pr_url"] == "https://github.com/org/test-app/pull/2"
        assert deliveries[0]["details"]["registered"] is False  # no GitOps infra repo registered in this test

    async def test_concurrent_calls_for_the_same_app_only_one_proceeds(self, agent_prs_client):
        """Per-Agent PRs shares the exact same fixed-branch-name +
        force-push-on-conflict shape as route_and_deliver()'s infra-repo
        commit path -- it must take the same per-app delivery lock."""
        client, store, aid = agent_prs_client
        await store.save_onboarding(aid, [_skill_file()])

        assert await store.claim_delivery_lock("delivery:test-app") is True
        with patch("agentit.portal.github_pr.create_agent_prs") as mock_create:
            resp = await client.post(f"/assessments/{aid}/create-agent-prs", follow_redirects=False)

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert "already%20in%20progress" in resp.headers["location"]
        mock_create.assert_not_called()
