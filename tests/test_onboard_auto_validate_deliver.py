"""Tests for onboarding's terminal behavior now that generation
automatically continues into ``auto_delivery.py``'s validate -> fix ->
final-review -> real-Deliver pipeline (replacing
``test_onboard_no_auto_deliver.py``, whose whole premise -- that onboarding
NEVER auto-delivers -- this change deliberately reverses, in a safer,
non-AutoMode shape: see auto_delivery.py's own module docstring for exactly
why this isn't a resurrection of the removed AutoMode/``auto_dry_run_then_
deliver()`` chain).

Covers:
  - onboard_submit() still has no ``auto_deliver`` Form field -- there is no
    "should this go automatically" decision to opt in/out of; the pipeline
    always runs.
  - ``_run_onboarding_job()`` calls the real ``auto_validate_and_deliver()``
    pipeline once manifests are generated, ending "completed" (a real PR
    opened) or "needs_attention" (validation/delivery couldn't finish on
    its own) -- never silently stuck at "manifests saved, nothing else
    happened".
  - The assess->onboard chain (assess_progress()) behaves identically.
  - The progress page/SSE stream never shows stale "will run automatically"
    pre-emptive messaging.
  - ``_onboard_terminal_redirect_url()``'s three-state behavior (failed /
    needs_attention / completed-with-auto_delivered-flag).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from agentit.portal.routes import assessments
from agentit.portal.services import onboard_pipeline
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


class TestOnboardSubmitHasNoAutoDeliverField:
    async def test_onboard_submit_has_no_auto_deliver_field(self, onboard_client):
        """There is no more "should this go automatically" decision to opt
        in/out of -- every Onboard run starts a plain onboarding job that
        always runs the full automatic pipeline once generation succeeds."""
        client, store = onboard_client
        aid = await _seed_assessment(store)

        resp = await client.post(f"/assessments/{aid}/onboard", data={}, follow_redirects=False)
        assert resp.status_code == 303
        job_id = resp.headers["location"].rsplit("/", 1)[1]
        job = await store.get_remediation_job(job_id)
        assert "auto_deliver" not in job["steps_completed"]


class TestRunOnboardingJobAutomaticallyValidatesAndDelivers:
    async def test_completes_and_opens_a_pr_when_validation_converges(self, onboard_client):
        """Generation succeeds, the manifest needs no fixing (a plain
        NetworkPolicy already satisfies every relevant property for this
        report's findings), so the job runs the full pipeline through to a
        real, non-dry-run route_and_deliver() and ends "completed" with a
        PR actually opened -- not stuck at "manifests saved"."""
        _client, store = onboard_client
        aid = await _seed_assessment(
            store, repo_name="onboard-completes-app",
            infra_repo_url="https://github.com/org/onboard-completes-app-gitops",
        )
        job_id = await store.create_remediation_job(aid)

        with patch.object(onboard_pipeline, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.github_pr.ensure_webhook", return_value={"created": False}), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo",
                   return_value={"pr_url": "https://github.com/org/onboard-completes-app-gitops/pull/1",
                                 "commit_url": "https://github.com/org/onboard-completes-app-gitops/commit/abc",
                                 "files_committed": 1}), \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            await onboard_pipeline._run_onboarding_job(job_id, aid, "http://testserver")

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"
        assert "ready for your approval" in job["current_step"]
        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) >= 1
        assert any(d["status"] in ("delivered", "partial") for d in deliveries)

    async def test_ends_needs_attention_when_validation_cannot_converge(self, onboard_client):
        """No GitOps infra repo at all -- a structural error nothing in the
        validate/fix loop can act on. The job must end "needs_attention"
        (manifests saved, human must finish by hand), and the real,
        non-dry-run commit must never be attempted."""
        _client, store = onboard_client
        aid = await _seed_assessment(store, repo_name="onboard-needs-attention-app", infra_repo_url=None)
        job_id = await store.create_remediation_job(aid)

        with patch.object(onboard_pipeline, "_run_onboarding", return_value=([_cluster_config_file()], _ORCH_SUMMARY_AUTO_APPROVE)), \
             patch("agentit.portal.github_pr.ensure_webhook", return_value={"created": False}), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            await onboard_pipeline._run_onboarding_job(job_id, aid, "http://testserver")

        job = await store.get_remediation_job(job_id)
        assert job["status"] == "needs_attention"
        assert job["error"]
        mock_commit.assert_not_called()
        # Best-effort manifests are saved regardless, for the manual fallback.
        saved = await store.get_onboarding(aid)
        assert saved is not None

    async def test_progress_page_never_shows_stale_pre_emptive_messaging(self, onboard_client):
        client, store = onboard_client
        aid = await _seed_assessment(store, repo_name="onboard-no-message-app")
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

        resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
        assert resp.status_code == 200
        assert "will run automatically" not in resp.text
        assert "Running automatic Dry Run" not in resp.text


class TestAssessOnboardChainNeverAutoDelivers:
    """The assess->onboard chain must behave identically to a plain manual
    Onboard -- it always starts a plain onboarding job (which itself now
    runs the automatic pipeline once generation succeeds). 2026-07-20:
    the chain is created deterministically by assess_submit()'s background
    thread (or webhook_assess()/webhook_github_push()) BEFORE the assess
    job is marked completed -- assess_progress() itself no longer creates
    anything, it only redirects to whatever onboard job already exists."""

    async def test_chained_onboard_job_has_no_auto_deliver_field(self, onboard_client):
        client, store = onboard_client
        job_id = await store.create_assessment_job("https://github.com/org/chain-app", continue_onboard=True)
        aid = await _seed_assessment(store, repo_name="chain-app")
        onboard_job_id = await store.create_remediation_job(aid)
        await store.update_assessment_job(job_id, "completed", "Assessment complete", assessment_id=aid)

        resp = await client.get(f"/assess/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].rsplit("/", 1)[1] == onboard_job_id
        job = await store.get_remediation_job(onboard_job_id)
        assert "auto_deliver" not in job["steps_completed"]


class TestOnboardTerminalRedirectUrl:
    """Direct unit coverage for the shared redirect decision
    (_onboard_terminal_redirect_url) -- three states now: failed (no
    manifests), needs_attention (manifests exist, pipeline couldn't
    finish), completed (a real PR was opened automatically)."""

    async def test_failed_goes_to_assessment_detail(self):
        job = {"status": "failed", "error": "boom"}
        url = await assessments._onboard_terminal_redirect_url("some-aid", job)
        assert url == "/assessments/some-aid?error=boom"
        assert "onboard-results" not in url

    async def test_needs_attention_goes_to_onboard_results_with_warning(self):
        job = {"status": "needs_attention", "error": "could not converge"}
        url = await assessments._onboard_terminal_redirect_url("some-aid", job)
        assert url.startswith("/assessments/some-aid/onboard-results?warning=")
        assert "could" in url

    async def test_completed_goes_to_onboard_results_with_auto_delivered_flag(self):
        job = {"status": "completed", "error": ""}
        url = await assessments._onboard_terminal_redirect_url("some-aid", job)
        assert url == "/assessments/some-aid/onboard-results?auto_delivered=true"
