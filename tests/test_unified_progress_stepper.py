"""Tests for the unified Scan progress stepper (Assess → Generate → Open PR).

Builds on the 2026-07-20 unify-two-workflow-pages fix (one shared
pipeline_stepper across assess/onboard progress). Founder follow-up:
humans should see three clear Scan stages, not a 9-step Cloning/…
roadmap that still felt like Assess vs Onboard dual pages.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.models import (
    ArchitectureInfo, AssessmentReport, DimensionScore, Finding,
    Language, Severity, StackInfo,
)
from agentit.portal.app import app
from conftest import make_store, prime_csrf

_ALL_STAGE_LABELS = (
    "Assess", "Generate", "Open PR / waiting for merge",
)


def _make_report(repo_name: str = "unified-stepper-app") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(languages=[Language(name="python", file_count=5, percentage=100.0)],
                         frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(service_count=1, architecture_style="monolith",
                                       has_api=True, api_style="REST", external_dependencies=[]),
        scores=[DimensionScore(dimension="security", score=70, max_score=100,
                                findings=[Finding(category="test", severity=Severity.low,
                                                   description="d", recommendation="r")])],
        criticality="medium", summary="s", remediation_plan=[],
    )


@pytest.fixture(autouse=True)
async def _override_store():
    test_store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=test_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=test_store):
        yield test_store


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


async def test_assess_progress_shows_full_unified_roadmap_when_chained(client, _override_store):
    """A chained Scan must show Assess → Generate → Open PR from first paint."""
    store = _override_store
    job_id = await store.create_assessment_job(
        "https://github.com/org/unified-chained-app", continue_onboard=True,
    )
    await store.update_assessment_job(job_id, "assessing", "Analyzing repository...")

    resp = await client.get(f"/assess/progress/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<h1>Scan</h1>" in html
    for label in _ALL_STAGE_LABELS:
        assert f">{label}<" in html, f"missing stage {label!r} from the unified roadmap"
    assert html.count("class=\"lifecycle-step ") == 3
    assert "lifecycle-step step-active" in html
    assert html.count("step-pending") == 2  # Generate + Open PR not yet reached


async def test_assess_progress_shows_only_assess_stage_when_not_chained(client, _override_store):
    """continue_onboard=0 must show only Assess, not Generate/PR placeholders."""
    store = _override_store
    job_id = await store.create_assessment_job(
        "https://github.com/org/unified-not-chained-app", continue_onboard=False,
    )
    await store.update_assessment_job(job_id, "cloning", "Cloning repository...")

    resp = await client.get(f"/assess/progress/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<h1>Scan</h1>" in html
    assert html.count("class=\"lifecycle-step ") == 1
    assert ">Assess<" in html
    assert "Generate" not in html
    assert "Open PR / waiting for merge" not in html


async def test_assess_progress_automatic_messaging_preserved(client, _override_store):
    """Chained Scan copy must still signal the automatic Generate → Open PR hand-off."""
    store = _override_store
    job_id = await store.create_assessment_job(
        "https://github.com/org/unified-auto-msg-app", continue_onboard=True,
    )
    await store.update_assessment_job(job_id, "cloning", "Cloning repository...")

    resp = await client.get(f"/assess/progress/{job_id}")
    assert "Generate and Open PR continue automatically" in resp.text
    assert "no new page to load" in resp.text


async def test_onboard_progress_page_shares_header_and_marks_assess_done(client, _override_store):
    """Standalone Generate-phase progress shares Scan header and marks Assess done."""
    store = _override_store
    aid = await store.save(_make_report("unified-onboard-standalone-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

    resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<h1>Scan</h1>" in html
    assert "Scan... - AgentIT" in html
    for label in _ALL_STAGE_LABELS:
        assert f">{label}<" in html
    assert html.count("class=\"lifecycle-step ") == 3
    assert html.count("lifecycle-step step-done") == 1  # Assess done
    assert "lifecycle-step step-active" in html
    assert "Agents" in html


async def test_onboard_progress_stream_marks_failed_step_and_keeps_recovery_link(client, _override_store):
    """Failed Generate still surfaces step-failed styling and Back-to-Assessment."""
    store = _override_store
    aid = await store.save(_make_report("unified-onboard-failed-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(
        job_id, "failed", "Onboarding failed: boom",
        error="Onboarding failed: boom -- no manifests were generated.",
    )

    async with client.stream("GET", f"/assessments/{aid}/onboard/progress/{job_id}/stream") as resp:
        assert resp.status_code == 200
        body = b"".join([chunk async for chunk in resp.aiter_bytes()])
    text = body.decode()
    assert "step-failed" in text
    assert "Onboarding failed: boom" in text
    assert "Back to Assessment" in text
    assert f"/assessments/{aid}?error=" in text
