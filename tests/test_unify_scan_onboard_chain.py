"""Tests for the deterministic, server-side assess->onboard->deliver chain
(2026-07-20). This continues directly from the Onboard/Scan button
investigation (PR #99), which found two real root causes but deliberately
left them unfixed:

  1. The interactive assess->onboard chain only ever fired when a browser
     stayed on (or returned to) ``GET /assess/progress/{job_id}`` long
     enough to claim the ``continue_onboard`` flag -- close the tab before
     the job finishes polling and the chain silently never fired.
  2. ``POST /api/webhook/assess`` (called by ``ReassessScheduler``'s
     cadence tick, a GitHub push, and Tekton's self-registration step) had
     no ``continue_onboard`` concept at all -- it could never chain into
     onboarding, full stop.

The product owner's directive: collapse Assessment Detail to one button
("Scan") and make it (and every other trigger) reliably run the full
assess -> onboard -> deliver chain automatically, server-side, regardless
of what triggered it or whether anyone's watching.

These tests prove the fix at the real-store level (``portal_client``
fixture -- a real Postgres-backed ``AssessmentStore``, not the in-memory
sync facade), following the exact polling-for-background-job-completion
pattern ``test_assess_onboard_default_chaining.py``/
``test_onboard_auto_validate_deliver.py`` already established.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentit.models import AssessmentReport
from agentit.portal.routes import assessments
from conftest import make_report


def _make_report(repo_name: str, *, infra_repo_url: str | None = None) -> AssessmentReport:
    """Delegates to conftest's `make_report()` (default finding category
    "test", not any of property_verifier's four gated categories --
    network/rbac/autoscaling/monitoring) so the single plain NetworkPolicy
    `_cluster_config_file()` below converges cleanly through the real
    validate/fix loop, matching test_onboard_auto_validate_deliver.py's own
    proven "completes and opens a PR" setup exactly."""
    report = make_report(repo_name=repo_name, criticality="medium")
    report.infra_repo_url = infra_repo_url
    return report


def _cluster_config_file(path: str = "netpol.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


_ORCH_SUMMARY = {"agents": [], "conflicts": [], "recommendation": "READY", "auto_approve": True, "gates": []}


async def _wait_for_job_terminal(store, job_id: str, *, max_wait: float = 20.0) -> dict:
    """Polls the real store directly (not any HTTP progress endpoint) until
    a remediation_jobs row reaches a terminal status -- same pattern
    test_assess_onboard_default_chaining.py's `_post_assess_and_wait_for_job`
    already uses for the assess job itself."""
    deadline = asyncio.get_running_loop().time() + max_wait
    job = None
    while asyncio.get_running_loop().time() < deadline:
        job = await store.get_remediation_job(job_id)
        assert job is not None, f"job {job_id} vanished"
        if job["status"] in ("completed", "failed", "needs_attention"):
            return job
        await asyncio.sleep(0.2)
    raise TimeoutError(f"job {job_id} never reached a terminal status: {job}")


async def _onboard_job_for(store, assessment_id: str, *, exclude_job_id: str | None = None) -> dict:
    jobs = await store.list_remediation_jobs(assessment_id)
    if exclude_job_id:
        jobs = [j for j in jobs if j["id"] != exclude_job_id]
    assert len(jobs) == 1, f"expected exactly one onboard job for {assessment_id}, got {jobs}"
    return jobs[0]


class TestManualAssessChainsToOpenPR:
    async def test_ui_triggered_assess_on_never_onboarded_app_ends_in_open_pr(self, portal_client):
        """(a) A manual, UI-triggered Scan/Assess on a never-onboarded app
        ends in an open PR with zero additional clicks -- no re-visit of
        any progress page required. Verified purely against real store
        state after the background job completes, not against any
        response body."""
        client, store, _seed_aid = portal_client
        pr_url = "https://github.com/org/manual-chain-app-gitops/pull/1"
        report = _make_report(
            "manual-chain-app", infra_repo_url="https://github.com/org/manual-chain-app-gitops",
        )

        with patch.object(assessments, "clone_repo", return_value=Path("/tmp/fake-manual-chain-repo")), \
             patch.object(assessments, "run_assessment", return_value=report), \
             patch.object(assessments, "_auto_create_infra_repo", return_value=report.infra_repo_url), \
             patch.object(assessments, "_run_onboarding",
                          return_value=([_cluster_config_file()], _ORCH_SUMMARY)), \
             patch("agentit.portal.github_pr.ensure_webhook", return_value={"created": False}), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo", return_value={
                 "pr_url": pr_url, "commit_url": pr_url.rsplit("/pull/", 1)[0] + "/commit/abc",
                 "files_committed": 1,
             }), \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            resp = await client.post(
                "/assess",
                data={"repo_url": report.repo_url, "criticality": "medium"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assess_job_id = resp.headers["location"].rsplit("/", 1)[1]

            assess_job = await _wait_for_job_terminal(store, assess_job_id)
            assert assess_job["status"] == "completed", assess_job
            assessment_id = assess_job["assessment_id"]
            assert assessment_id

            onboard_job = await _onboard_job_for(store, assessment_id, exclude_job_id=assess_job_id)
            onboard_job = await _wait_for_job_terminal(store, onboard_job["id"])

        assert onboard_job["status"] == "completed", onboard_job
        assert "pull request" in onboard_job["current_step"]

        deliveries = await store.list_deliveries(assessment_id)
        assert deliveries and any(d["status"] in ("delivered", "partial") for d in deliveries)
        onboardings = await store.list_onboardings(assessment_id)
        assert onboardings

        detail = await client.get(f"/assessments/{assessment_id}")
        assert "Onboard This App" not in detail.text


class TestWebhookAssessChainsToOpenPR:
    async def test_webhook_triggered_assess_ends_in_open_pr(self, portal_client):
        """(b) POST /api/webhook/assess -- the exact route ReassessScheduler's
        cadence tick, a GitHub push, and Tekton's self-registration step all
        call -- must chain into onboarding and end in an open PR on its own,
        the same as a manual UI Scan. This is the test that could not pass
        before this fix (webhook_assess() had no continue_onboard concept
        at all)."""
        client, store, _seed_aid = portal_client
        pr_url = "https://github.com/org/webhook-chain-app-gitops/pull/7"
        report = _make_report(
            "webhook-chain-app", infra_repo_url="https://github.com/org/webhook-chain-app-gitops",
        )

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=report), \
             patch.object(assessments, "_run_onboarding",
                          return_value=([_cluster_config_file()], _ORCH_SUMMARY)), \
             patch("agentit.portal.github_pr.ensure_webhook", return_value={"created": False}), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch("agentit.portal.github_pr.commit_to_infra_repo", return_value={
                 "pr_url": pr_url, "commit_url": pr_url.rsplit("/pull/", 1)[0] + "/commit/abc",
                 "files_committed": 1,
             }), \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            resp = await client.post(
                "/api/webhook/assess",
                json={"repo_url": report.repo_url, "criticality": "medium"},
            )
            assert resp.status_code == 200
            assessment_id = resp.json()["assessment_id"]
            assert assessment_id

            onboard_job = await _onboard_job_for(store, assessment_id)
            onboard_job = await _wait_for_job_terminal(store, onboard_job["id"])

        assert onboard_job["status"] == "completed", onboard_job
        deliveries = await store.list_deliveries(assessment_id)
        assert deliveries and any(d["status"] in ("delivered", "partial") for d in deliveries)


class TestCadenceReassessOfUnchangedOnboardedAppDoesNotSpam:
    async def test_repeated_webhook_reassess_of_unchanged_app_does_not_open_duplicate_pr(self, portal_client):
        """(c) A repeated, cadence-triggered re-assess (POST /api/webhook/assess,
        the same route ReassessScheduler's tick uses) of an app that's
        already onboarded and whose generated manifests are byte-identical
        to what's already committed must not spam a duplicate PR, and must
        not surface a false "needs your attention" failure -- relies on
        github_pr.py's `_infra_repo_content_unchanged()` dedup (closing the
        gap `commit_to_infra_repo()` never had, unlike `create_agent_prs()`).
        `commit_to_infra_repo()` itself is exercised for real here (only its
        `requests` calls are faked), so this proves the real dedup code
        path, not a hand-substituted mock."""
        client, store, _seed_aid = portal_client
        infra_repo_url = "https://github.com/org/steady-app-gitops"
        report = _make_report("steady-app", infra_repo_url=infra_repo_url)
        deployed_content = _cluster_config_file()["content"]

        def _mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if url.endswith("/repos/org/steady-app-gitops"):
                resp.json.return_value = {"default_branch": "main"}
            elif "git/ref/heads/main" in url:
                resp.json.return_value = {"object": {"sha": "sha-main-1"}}
            elif url.endswith("/contents/apps/steady-app/skills/netpol.yaml"):
                resp.json.return_value = {"content": base64.b64encode(deployed_content.encode()).decode()}
            return resp

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=report), \
             patch.object(assessments, "_run_onboarding",
                          return_value=([_cluster_config_file()], _ORCH_SUMMARY)), \
             patch("agentit.portal.github_pr.ensure_webhook", return_value={"created": False}), \
             patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"}), \
             patch("agentit.portal.github_pr.requests") as mock_requests:
            mock_requests.get.side_effect = _mock_get

            resp = await client.post(
                "/api/webhook/assess",
                json={"repo_url": report.repo_url, "criticality": "medium"},
            )
            assert resp.status_code == 200
            assessment_id = resp.json()["assessment_id"]

            onboard_job = await _onboard_job_for(store, assessment_id)
            onboard_job = await _wait_for_job_terminal(store, onboard_job["id"])

        # Not a failure: the automatic pipeline honestly no-ops because
        # nothing changed -- never "needs_attention"/"failed".
        assert onboard_job["status"] == "completed", onboard_job
        assert "nothing new to deliver" in onboard_job["current_step"]
        # No commit/PR-opening POST was ever made -- the dedup short-
        # circuited before any mutating GitHub API call.
        mock_requests.post.assert_not_called()

        deliveries = await store.list_deliveries(assessment_id)
        assert deliveries
        assert all(
            not (isinstance(o, dict) and o.get("pr_url"))
            for d in deliveries
            for o in (d.get("details") or {}).get("outcomes", {}).values()
        )


class TestAssessmentDetailSingleButton:
    async def test_onboard_this_app_button_no_longer_renders_in_normal_state(self, portal_client):
        """(d) 'Onboard This App' no longer renders as a separate,
        permanently-visible button on Assessment Detail in the normal
        (healthy, no-error) state -- Scan is the one action."""
        client, store, _seed_aid = portal_client
        aid = await store.save(_make_report("single-button-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Onboard This App" not in resp.text
        assert "btn-label\">Scan</span>" in resp.text
