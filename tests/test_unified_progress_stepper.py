"""Tests for the unified assess->onboard progress stepper (2026-07-20).

The product owner's complaint: "why are we showing two workflow pages,
this should be just one" -- a 3-step "Running Assessment" stepper handing
off to an unrelated 6-step "Onboarding" stepper. Investigation (see
routes/assessments.py's module comments above `_assess_pipeline_position`/
`_onboard_pipeline_position`) found the hand-off between the two real
jobs/routes was ALREADY a same-page htmx AJAX swap, never a real browser
navigation -- the "two workflow pages" feel was purely visual: two
differently-shaped, differently-titled steppers swapped in and out of the
same `#main-content` container.

This fix does not merge the two underlying jobs/routes/redirect (still
two real `remediation_jobs` rows, still a real 303 from assess_progress()
to onboard_progress() once an onboard job exists) -- see
test_portal.py::test_assess_progress_redirects_to_already_existing_onboard_job
and test_assess_onboard_default_chaining.py, both still passing unchanged.
It unifies ONLY the presentation: one `pipeline_stepper()` macro
(_macros.html), one consistent "Onboarding" header/title, rendered on
both assess_progress.html and _onboard_progress_fragment.html, so the
swap reads as one page's steps lighting up in sequence.
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
    "Cloning", "Analyzing", "Saving Score",
    "Running Agents", "Saving Manifests", "Validating",
    "Final Review", "Creating PR(s)", "Done",
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
    """A chained (continue_onboard=1, the default) assess job must show the
    FULL 9-stage roadmap from its very first render -- not a 3-step
    stepper that later gets thrown away for an unrelated one."""
    store = _override_store
    job_id = await store.create_assessment_job(
        "https://github.com/org/unified-chained-app", continue_onboard=True,
    )
    await store.update_assessment_job(job_id, "assessing", "Analyzing repository...")

    resp = await client.get(f"/assess/progress/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<h1>Onboarding</h1>" in html
    for label in _ALL_STAGE_LABELS:
        assert f">{label}<" in html, f"missing stage {label!r} from the unified roadmap"
    # "Analyzing" (index 1) is the real active step; the stepper never
    # fabricates progress on stages this job hasn't reached yet.
    assert html.count("class=\"lifecycle-step ") == 9
    assert html.count("step-pending") >= 6  # the 6 onboarding stages, not yet reached


async def test_assess_progress_shows_only_assess_stages_when_not_chained(client, _override_store):
    """continue_onboard=0 (still a real opt-out -- see assess_submit())
    must show only the 3 stages this job will ever actually reach, not 6
    onboarding placeholders that will never light up."""
    store = _override_store
    job_id = await store.create_assessment_job(
        "https://github.com/org/unified-not-chained-app", continue_onboard=False,
    )
    await store.update_assessment_job(job_id, "cloning", "Cloning repository...")

    resp = await client.get(f"/assess/progress/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<h1>Onboarding</h1>" in html
    assert html.count("class=\"lifecycle-step ") == 3
    assert ">Cloning<" in html and ">Analyzing<" in html and ">Saving Score<" in html
    assert "Running Agents" not in html


async def test_assess_progress_automatic_messaging_preserved(client, _override_store):
    """The 'onboarding will start automatically' signal (relied on by
    test_assess_onboard_default_chaining.py) must still fire verbatim --
    strengthened, not replaced, now that it's visually one page."""
    store = _override_store
    job_id = await store.create_assessment_job(
        "https://github.com/org/unified-auto-msg-app", continue_onboard=True,
    )
    await store.update_assessment_job(job_id, "cloning", "Cloning repository...")

    resp = await client.get(f"/assess/progress/{job_id}")
    assert "onboarding will start automatically" in resp.text
    assert "no new page to load" in resp.text


async def test_onboard_progress_page_shares_header_and_marks_assess_stages_done(client, _override_store):
    """The standalone onboard progress page (reached directly from
    Retry Onboard / Run Validation / assessment_detail.html's 'live
    progress' link, with no assess phase in this session at all) must
    render the exact same header/title and the SAME 9-stage roadmap as
    assess_progress.html, with stages 0-2 already marked done -- an
    onboard job only ever exists for an assessment that already
    completed."""
    store = _override_store
    aid = await store.save(_make_report("unified-onboard-standalone-app"))
    job_id = await store.create_remediation_job(aid)
    await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

    resp = await client.get(f"/assessments/{aid}/onboard/progress/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<h1>Onboarding</h1>" in html
    assert "Onboarding... - AgentIT" in html
    for label in _ALL_STAGE_LABELS:
        assert f">{label}<" in html
    assert html.count("class=\"lifecycle-step ") == 9
    # Cloning/Analyzing/Saving Score are done; Running Agents is active.
    assert html.count("lifecycle-step step-done") == 3
    assert "lifecycle-step step-active" in html
    # The live per-agent results list this fix must preserve verbatim.
    assert "Agents" in html


async def test_onboard_progress_stream_marks_failed_step_and_keeps_recovery_link(client, _override_store):
    """A genuinely failed onboard job must still surface `step-failed`
    styling and the existing Back-to-Assessment recovery link -- the
    unified stepper must not lose today's error-state handling while
    consolidating the happy path."""
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
    assert f"/assessments/{aid}?error=" in text  # the redirect script's target
