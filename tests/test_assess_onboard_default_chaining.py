"""Tests for assess->onboard chaining becoming the default for every Assess
-- docs/onboarding-loop-vision-gap-analysis.md Phase 0 item 5.

Before this fix, `assess_submit()` only chained into onboarding when a
caller explicitly posted `continue_onboard=1` (in practice, only Fleet's
"Refresh Onboard" button for already-onboarded apps). This proves a plain,
fresh Assess -- the "New Assessment" modal / command-palette / "Re-assess"
path, none of which ever set that field -- now chains by default too, and
that the existing "onboarding will start automatically" signal
(`assess_progress.html`) fires for it, not just Refresh Onboard.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agentit.models import (
    ArchitectureInfo, AssessmentReport, DimensionScore, Finding,
    Language, Severity, StackInfo,
)
from agentit.portal.services import assess_pipeline


def _make_report(repo_name: str = "fresh-assess-chain-app") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(languages=[Language(name="python", file_count=5, percentage=100.0)],
                         frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(service_count=1, architecture_style="monolith",
                                       has_api=True, api_style="REST", external_dependencies=[]),
        scores=[DimensionScore(dimension="security", score=70, max_score=100,
                                findings=[Finding(category="test", severity=Severity.low,
                                                   description="d", recommendation="r")])],
        criticality="medium", summary="s", remediation_plan=[],
    )


async def _post_assess_and_wait_for_job(client, store, report) -> dict:
    """POSTs to /assess through the real ASGI app -- deliberately never
    including a `continue_onboard` field in the form body, the same shape
    every real caller that isn't Fleet's "Refresh Onboard" button already
    sends -- so FastAPI's own Form-default binding is what's under test,
    not a Python-level default a direct coroutine call could paper over."""
    with patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake-fresh-assess-repo")), \
         patch.object(assess_pipeline, "run_assessment", return_value=report), \
         patch.object(assess_pipeline, "_auto_create_infra_repo",
                      return_value="https://github.com/org/fresh-assess-chain-app-gitops"):
        resp = await client.post(
            "/assess",
            data={"repo_url": report.repo_url, "criticality": "medium"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].split("/assess/progress/")[1]

        deadline = asyncio.get_running_loop().time() + 15.0
        job = None
        while asyncio.get_running_loop().time() < deadline:
            job = await store.get_remediation_job(job_id)
            assert job is not None, f"job {job_id} vanished"
            if job["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.2)
    assert job is not None and job["status"] == "completed", f"assess job did not complete: {job}"
    return job


class TestFreshAssessChainsByDefault:
    async def test_fresh_assess_with_no_continue_onboard_field_still_chains(self, portal_client):
        client, store, _seed_aid = portal_client
        report = _make_report()

        job = await _post_assess_and_wait_for_job(client, store, report)

        # The chain decision is recorded at job-creation time
        # (create_assessment_job(..., continue_onboard=chain)) -- this is
        # the durable, queryable proof the server chose to chain, entirely
        # independent of whether the onboarding job itself later succeeds.
        assert "continue_onboard" in job["steps_completed"]

    async def test_progress_page_shows_automatic_onboarding_signal(self, portal_client):
        """The visible-indication requirement: assess_progress.html's
        existing "onboarding will start automatically" message must appear
        for this plain Assess too, not just Refresh Onboard."""
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="fresh-assess-signal-app")

        with patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake-fresh-signal-repo")), \
             patch.object(assess_pipeline, "run_assessment", return_value=report), \
             patch.object(assess_pipeline, "_auto_create_infra_repo",
                          return_value="https://github.com/org/fresh-assess-signal-app-gitops"):
            resp = await client.post(
                "/assess",
                data={"repo_url": report.repo_url, "criticality": "medium"},
                follow_redirects=False,
            )
            job_id = resp.headers["location"].split("/assess/progress/")[1]

            progress_resp = await client.get(f"/assess/progress/{job_id}")
        assert progress_resp.status_code == 200
        assert "onboarding will start automatically" in progress_resp.text

    async def test_completed_fresh_assess_redirects_into_onboarding_progress(self, portal_client):
        """Once scoring completes, /assess/progress/{job_id} must redirect
        into onboarding progress (claim_continue_onboard fired) rather than
        landing on plain Assessment Detail -- proving the chain wasn't
        just recorded but actually acted on."""
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="fresh-assess-redirect-app")

        job = await _post_assess_and_wait_for_job(client, store, report)
        job_id = job["id"]

        resp = await client.get(f"/assess/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        assert "/onboard/progress/" in resp.headers["location"]

    async def test_explicit_opt_out_still_works(self, portal_client):
        """The mechanism to opt out (continue_onboard=0/false/"") stays
        available even though nothing sets it today -- posting it
        explicitly must not chain."""
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="fresh-assess-opt-out-app")

        with patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake-opt-out-repo")), \
             patch.object(assess_pipeline, "run_assessment", return_value=report), \
             patch.object(assess_pipeline, "_auto_create_infra_repo",
                          return_value="https://github.com/org/fresh-assess-opt-out-app-gitops"):
            resp = await client.post(
                "/assess",
                data={"repo_url": report.repo_url, "criticality": "medium", "continue_onboard": "0"},
                follow_redirects=False,
            )
            job_id = resp.headers["location"].split("/assess/progress/")[1]
            deadline = asyncio.get_running_loop().time() + 15.0
            job = None
            while asyncio.get_running_loop().time() < deadline:
                job = await store.get_remediation_job(job_id)
                if job["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.2)
        assert job["status"] == "completed"
        assert "continue_onboard" not in job["steps_completed"]

        resp = await client.get(f"/assess/progress/{job_id}", follow_redirects=False)
        assert resp.status_code == 303
        assert "/onboard/progress/" not in resp.headers["location"]
