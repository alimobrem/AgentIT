"""Tests for onboarding's terminal behavior now that AutoMode -- and the
automatic Dry Run -> Deliver chain it used to gate
(docs/onboarding-loop-vision-gap-analysis.md Phase 3) -- has been removed.

Onboarding now always stops at "completed" once manifests are generated
and saved, regardless of the orchestrator's auto_approve plan or LLM
availability -- a human always clicks Deliver on Onboard Results to
proceed, consistent with every other GitOps delivery in this app (which
always needs a human to merge the resulting PR anyway).

Covers (redirected from the removed test_onboard_auto_deliver_chain.py):
  - onboard_submit() has no more auto_deliver Form field -- every Onboard
    run reaches plain "completed", never attempts route_and_deliver.
  - The assess->onboard chain (assess_progress()) behaves identically.
  - The progress page/SSE stream never shows "will run automatically"
    messaging and never branches on a removed job status.
  - _onboard_terminal_redirect_url()'s simplified two-state behavior.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from agentit.portal.routes import assessments
from conftest import make_report, make_store, prime_csrf


def _cluster_config_file(path: str = "netpol.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


_ORCH_SUMMARY_AUTO_APPROVE = {
    "agents": [], "conflicts": [], "recommendation": "READY", "auto_approve": True, "gates": [],
}


@pytest.fixture
async def onboard_client():
    store = await make_store()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
        await prime_csrf(client)
        with patch("agentit.portal.app.get_store", return_value=store), \
             patch("agentit.portal.routes.assessments.get_store", return_value=store):
            yield client, store


async def _seed_assessment(store, *, repo_name: str = "onboard-app", infra_repo_url: str | None = None) -> str:
    report = make_report(repo_name=repo_name)
    report.infra_repo_url = infra_repo_url
    return await store.save(report)


class TestOnboardNeverAutoDelivers:
    async def test_onboard_submit_has_no_auto_deliver_field(self, onboard_client):
        """AutoMode has been removed -- there is no more Form field to
        opt in/out of an automatic chain with; every Onboard run just
        starts a plain onboarding job."""
        client, store = onboard_client
        aid = await _seed_assessment(store)

        resp = await client.post(f"/assessments/{aid}/onboard", data={}, follow_redirects=False)
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        job = await store.get_remediation_job(job_id)
        assert "auto_deliver" not in job["steps_completed"]

    async def test_run_onboarding_job_always_completes_without_delivering(self, onboard_client):
        """Even when the orchestrator's plan auto-approves and an LLM is
        available (the exact conditions that used to auto-deliver), the
        job always reaches plain "completed" and never calls
        route_and_deliver at all."""
        _client, store = onboard_client
        aid = await _seed_assessment(
            store, repo_name="onboard-completes-app",
            infra_repo_url="https://github.com/org/onboard-completes-app-gitops",
        )
        job_id = await store.create_remediation_job(aid)

        with patch.object(assessments, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.delivery.route_and_deliver") as mock_route:
            await assessments._run_onboarding_job(job_id, aid, "http://testserver")

        mock_route.assert_not_called()
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"
        deliveries = await store.list_deliveries(aid)
        assert deliveries == []

    async def test_progress_page_never_shows_automatic_chaining_message(self, onboard_client):
        client, store = onboard_client
        aid = await _seed_assessment(store, repo_name="onboard-no-message-app")
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
        assert resp.status_code == 200
        assert "will run automatically" not in resp.text
        assert "Running automatic Dry Run" not in resp.text


class TestAssessOnboardChainNeverAutoDelivers:
    """assess_progress()'s assess->onboard chain must behave identically --
    it always starts a plain onboarding job now too."""

    async def test_chained_onboard_job_has_no_auto_deliver_field(self, onboard_client):
        client, store = onboard_client
        job_id = await store.create_assessment_job("https://github.com/org/chain-app", continue_onboard=True)
        aid = await _seed_assessment(store, repo_name="chain-app")
        await store.update_assessment_job(job_id, "completed", "Assessment complete", assessment_id=aid)

        resp = await client.get(f"/assess/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        onboard_job_id = resp.headers["location"].rsplit("/", 1)[1]
        job = await store.get_remediation_job(onboard_job_id)
        assert "auto_deliver" not in job["steps_completed"]


class TestOnboardTerminalRedirectUrl:
    """Direct unit coverage for the simplified shared redirect decision
    (_onboard_terminal_redirect_url) -- two states now, not five."""

    async def test_failed_goes_to_assessment_detail(self):
        job = {"status": "failed", "error": "boom"}
        url = await assessments._onboard_terminal_redirect_url("some-aid", job)
        assert url == "/assessments/some-aid?error=boom"
        assert "onboard-results" not in url

    async def test_completed_is_a_bare_redirect_to_onboard_results(self):
        job = {"status": "completed", "error": ""}
        url = await assessments._onboard_terminal_redirect_url("some-aid", job)
        assert url == "/assessments/some-aid/onboard-results"
