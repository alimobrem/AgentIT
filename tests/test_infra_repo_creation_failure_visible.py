"""Tests for surfacing a silent `_auto_create_infra_repo()` failure on the
main `assess_submit` background-job path --
docs/onboarding-loop-vision-gap-analysis.md Phase 0 item 4, and, since GitOps
registration became mandatory (all apps must use GitOps -- no Direct Apply
fallback), the hard-stop upgrade of that same failure path.

Originally, `_auto_create_infra_repo()`'s failure only got a `logger.warning()`
-- a server log line nobody watching the portal ever sees. 9e036d9 first
fixed that to a real logged event, but the assessment still completed and
saved with no `infra_repo_url` (silently falling back to Direct Apply). Now
that Direct Apply is being removed as a concept entirely, this same failure
must hard-stop the whole Assess job -- no assessment is saved at all -- with
the same real, queryable, user-facing signal (event + actionable job-failure
message) as before, just as a failure instead of a soft warning.
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
from agentit.portal.routes import assessments
from agentit.portal.services import assess_pipeline
from conftest import make_store


def _make_report(repo_name: str = "infra-fail-app") -> AssessmentReport:
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
        infra_repo_url=None,
    )


async def _submit_and_wait_for_terminal_status(pg_store, report, *, auto_create_infra_return) -> dict:
    with patch.object(assessments, "get_store", return_value=pg_store), \
         patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake-infra-fail-repo")), \
         patch.object(assess_pipeline, "run_assessment", return_value=report), \
         patch.object(assess_pipeline, "_auto_create_infra_repo", return_value=auto_create_infra_return):
        response = await assessments.assess_submit(
            request=None, repo_url=report.repo_url, criticality="medium",
            infra_repo_url="", continue_onboard="",
        )
        assert response.status_code == 303
        job_id = response.headers["location"].split("/assess/progress/")[1]

        deadline = asyncio.get_running_loop().time() + 15.0
        job = None
        while asyncio.get_running_loop().time() < deadline:
            job = await pg_store.get_remediation_job(job_id)
            assert job is not None, f"job {job_id} vanished"
            if job["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.2)
    assert job is not None, "assess job never reached a terminal status"
    return job


class TestInfraRepoCreationFailureHardStopsAssess:
    async def test_failed_auto_create_hard_stops_the_job_no_direct_apply_fallback(self):
        pg_store = await make_store()
        report = _make_report()

        job = await _submit_and_wait_for_terminal_status(pg_store, report, auto_create_infra_return=None)

        assert job["status"] == "failed", job
        assert not job.get("assessment_id"), "no assessment should ever be saved for a blocked Assess"
        assert "GitOps" in job["current_step"] and "no Direct Apply fallback" in job["current_step"]

        # No assessment_id exists to correlate to (the block fires before the
        # pipeline runs at all) -- the event is still real and queryable,
        # keyed by the app name parsed straight from repo_url.
        events = await pg_store.list_events(target_app=report.repo_name)
        matching = [e for e in events if e["action"] == "infra-repo-creation-failed"]
        assert len(matching) == 1, events
        assert matching[0]["severity"] == "critical"
        assert matching[0]["correlation_id"] is None

    async def test_successful_auto_create_completes_normally(self):
        """Regression guard: a real infra_repo_url (auto-create succeeded)
        must not be blocked -- the assessment completes and saves exactly as
        before."""
        pg_store = await make_store()
        report = _make_report(repo_name="infra-success-app")
        report.infra_repo_url = "https://github.com/org/agentit-gitops"

        job = await _submit_and_wait_for_terminal_status(
            pg_store, report, auto_create_infra_return=report.infra_repo_url,
        )

        assert job["status"] == "completed"
        assert job.get("assessment_id")

        events = await pg_store.list_events(target_app=report.repo_name)
        assert not [e for e in events if e["action"] == "infra-repo-creation-failed"]


class TestAssessmentDetailShowsFailureBanner:
    async def test_banner_shown_after_auto_create_failure(self, portal_client):
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="infra-fail-banner-app")
        aid = await store.save(report)
        await store.log_event(
            "portal", "infra-repo-creation-failed", report.repo_name, "warning",
            "Could not auto-create a GitOps infra repo for this app during assessment.",
            correlation_id=aid,
        )

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # Crisp IA: shortened notice copy inside the Notices collapse.
        assert "Auto-create of a GitOps infra repo failed" in resp.text

    async def test_no_banner_when_no_failure_event(self, portal_client):
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="infra-no-fail-banner-app")
        aid = await store.save(report)

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Auto-create of a GitOps infra repo failed" not in resp.text

    async def test_banner_cleared_after_later_successful_registration(self, portal_client):
        """A later successful "gitops-registered" event (e.g. a manual
        Register-for-GitOps retry) must clear the banner -- it's the most
        *recent* fact about this app's infra-repo state that matters, not
        just "did a failure ever happen"."""
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="infra-fail-then-fixed-app")
        aid = await store.save(report)
        await store.log_event(
            "portal", "infra-repo-creation-failed", report.repo_name, "warning",
            "Could not auto-create a GitOps infra repo.",
            correlation_id=aid,
        )
        await store.log_event(
            "portal", "gitops-registered", report.repo_name, "info",
            "Registered for GitOps delivery via https://github.com/org/agentit-gitops",
            correlation_id=aid,
        )

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Auto-create of a GitOps infra repo failed" not in resp.text
