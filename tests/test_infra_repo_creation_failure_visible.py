"""Tests for surfacing a silent `_auto_create_infra_repo()` failure on the
main `assess_submit` background-job path --
docs/onboarding-loop-vision-gap-analysis.md Phase 0 item 4.

Before this fix, `_auto_create_infra_repo()` swallowed every exception with
only a `logger.warning()` call -- a server log line nobody watching the
portal ever sees -- unlike the standalone `register_gitops()` retry route,
which redirects with a real `?error=` flash. This proves the primary path
now produces a real, queryable, user-facing signal too: a logged event
(visible on Events/Ledger) and a dedicated banner on Assessment Detail.
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


async def _run_assess_submit_and_wait(pg_store, report) -> dict:
    with patch.object(assessments, "get_store", return_value=pg_store), \
         patch.object(assessments, "clone_repo", return_value=Path("/tmp/fake-infra-fail-repo")), \
         patch.object(assessments, "run_assessment", return_value=report), \
         patch.object(assessments, "_auto_create_infra_repo", return_value=None):
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
    assert job is not None and job["status"] == "completed", f"assess job did not complete: {job}"
    return job


class TestInfraRepoCreationFailureLogsRealEvent:
    async def test_failed_auto_create_logs_a_visible_event(self):
        pg_store = await make_store()
        report = _make_report()

        job = await _run_assess_submit_and_wait(pg_store, report)
        assessment_id = job["assessment_id"]

        events = await pg_store.list_events(target_app=report.repo_name)
        matching = [e for e in events if e["action"] == "infra-repo-creation-failed"]
        assert len(matching) == 1, events
        assert matching[0]["severity"] == "warning"
        assert matching[0]["correlation_id"] == assessment_id

    async def test_successful_auto_create_logs_no_failure_event(self):
        """Regression guard: a real infra_repo_url on the saved report
        (auto-create succeeded) must not falsely flag a failure."""
        pg_store = await make_store()
        report = _make_report(repo_name="infra-success-app")
        report.infra_repo_url = "https://github.com/org/agentit-gitops"

        with patch.object(assessments, "get_store", return_value=pg_store), \
             patch.object(assessments, "clone_repo", return_value=Path("/tmp/fake-infra-success-repo")), \
             patch.object(assessments, "run_assessment", return_value=report), \
             patch.object(assessments, "_auto_create_infra_repo", return_value=report.infra_repo_url):
            response = await assessments.assess_submit(
                request=None, repo_url=report.repo_url, criticality="medium",
                infra_repo_url="", continue_onboard="",
            )
            job_id = response.headers["location"].split("/assess/progress/")[1]
            deadline = asyncio.get_running_loop().time() + 15.0
            job = None
            while asyncio.get_running_loop().time() < deadline:
                job = await pg_store.get_remediation_job(job_id)
                if job["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.2)
        assert job["status"] == "completed"

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
        assert "tried to auto-create a GitOps infra repo" in resp.text

    async def test_no_banner_when_no_failure_event(self, portal_client):
        client, store, _seed_aid = portal_client
        report = _make_report(repo_name="infra-no-fail-banner-app")
        aid = await store.save(report)

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "tried to auto-create a GitOps infra repo" not in resp.text

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
        assert "tried to auto-create a GitOps infra repo" not in resp.text
