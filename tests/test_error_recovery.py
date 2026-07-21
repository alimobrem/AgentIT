from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)
from agentit.portal.app import app, get_store
from agentit.portal.services import assess_pipeline
from conftest import make_store, prime_csrf


def _make_report_with_findings(repo_name: str = "error-repo") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", version="3.12", file_count=10, percentage=100.0)],
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=["pip"],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=[
            DimensionScore(
                dimension="security",
                score=30,
                max_score=100,
                findings=[
                    Finding(
                        category="network security",
                        severity=Severity.high,
                        description="No network policy",
                        recommendation="Add NetworkPolicy",
                    ),
                    Finding(
                        category="container security",
                        severity=Severity.high,
                        description="No Containerfile found",
                        recommendation="Add Containerfile",
                    ),
                ],
            ),
            DimensionScore(
                dimension="observability",
                score=20,
                max_score=100,
                findings=[
                    Finding(
                        category="metrics endpoint",
                        severity=Severity.medium,
                        description="No metrics endpoint",
                        recommendation="Add /metrics",
                    ),
                ],
            ),
            DimensionScore(
                dimension="cicd",
                score=25,
                max_score=100,
                findings=[
                    Finding(
                        category="pipeline cicd",
                        severity=Severity.high,
                        description="No CI/CD pipeline",
                        recommendation="Add Tekton pipeline",
                    ),
                ],
            ),
            DimensionScore(
                dimension="compliance",
                score=35,
                max_score=100,
                findings=[
                    Finding(
                        category="policy compliance",
                        severity=Severity.medium,
                        description="No admission policies",
                        recommendation="Add Kyverno policies",
                    ),
                ],
            ),
        ],
        criticality="high",
        summary="Needs onboarding",
        remediation_plan=[],
    )


@pytest.fixture(autouse=True)
async def _override_store():
    """Patch get_store, and image_builder.build_app_image (see test_portal.py's
    identical fixture for why: onboarding here would otherwise shell out to a
    real `oc apply` against whatever cluster the local kubeconfig points to)."""
    test_store = await make_store()
    async_store = test_store
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=async_store), \
         patch("agentit.image_builder.build_app_image",
               return_value={"image_ref": "test/image:test", "run_name": "test-run", "status": "skipped-in-tests"}):
        yield test_store


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


# ------------------------------------------------------------------
# 1. Agent crash returns partial results
# ------------------------------------------------------------------


async def test_onboard_agent_crash_returns_partial(client, _override_store):
    """When one agent (CostOptimizationAgent) raises, others still produce files.

    HardeningAgent (and security/observability/cicd/compliance generally)
    were removed once skills covered their domains (see
    docs/agent-removal-readiness.md) -- "cost" is one of the three Python
    agents left, so it's the one this regression guard crashes now.
    """
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    with patch(
        "agentit.agents.cost.CostOptimizationAgent.run",
        side_effect=RuntimeError("simulated crash"),
    ):
        resp = await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)

    assert resp.status_code == 303
    # Onboard now runs as a background job with a real-time progress page
    # (docs/ux-design-requirements.md checklist #6/#8).
    assert f"/assessments/{aid}/onboard/progress/" in resp.headers["location"]

    files = await store.get_onboarding(aid)
    assert files is not None
    assert len(files) > 0, "Other agents/skills should still produce files despite cost agent crash"

    categories = {f["category"] for f in files}
    assert "cost" not in categories, "Crashed agent should not produce output"
    # At least one other source (skills, or a surviving Python agent) succeeded
    assert categories & {"skills", "dependency", "codechange"}


# ------------------------------------------------------------------
# 2. Webhook onboard failure returns 500
# ------------------------------------------------------------------


async def test_webhook_onboard_failure_returns_500(client, _override_store):
    """POST /api/webhook/onboard returns 500 when _run_onboarding raises."""
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    with patch(
        "agentit.portal.routes.webhooks.run_onboarding",
        side_effect=Exception("agent crashed"),
    ):
        resp = await client.post(
            "/api/webhook/onboard",
            json={"correlationId": aid},
        )

    assert resp.status_code == 500
    data = resp.json()
    assert "error" in data
    assert "agent crashed" in data["error"]
    assert data["assessment_id"] == aid


# ------------------------------------------------------------------
# 3. Store concurrent saves / isolation
# ------------------------------------------------------------------


async def test_store_concurrent_saves(_override_store):
    """Two reports with different repo_names coexist in the same store."""
    store = _override_store
    report_a = _make_report_with_findings("repo-alpha")
    report_b = _make_report_with_findings("repo-beta")

    aid_a = await store.save(report_a)
    aid_b = await store.save(report_b)

    all_assessments = await store.list_all()
    assert len(all_assessments) == 2
    names = {a["repo_name"] for a in all_assessments}
    assert names == {"repo-alpha", "repo-beta"}

    got_a = await store.get(aid_a)
    got_b = await store.get(aid_b)
    assert got_a is not None
    assert got_b is not None
    assert got_a.repo_name == "repo-alpha"
    assert got_b.repo_name == "repo-beta"


# ------------------------------------------------------------------
# 4. Store event logging on assessment save
# ------------------------------------------------------------------


async def test_store_event_logging_on_assessment(_override_store):
    """await store.save() dual-writes an event with action containing 'assessment'."""
    store = _override_store
    report = _make_report_with_findings("event-repo")
    await store.save(report)

    events = await store.list_events()
    assert len(events) > 0

    assessment_events = [e for e in events if "assessment" in e["action"]]
    assert len(assessment_events) >= 1, (
        f"Expected at least one event with 'assessment' in action, got: "
        f"{[e['action'] for e in events]}"
    )


# ------------------------------------------------------------------
# 5. Portal LLM unavailable -- graceful degradation
# ------------------------------------------------------------------


async def _poll_assess_progress(client, job_id: str, max_wait: float = 5.0) -> str:
    """Poll /assess/progress/{job_id} until it redirects to /assessments/{id}."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        resp = await client.get(f"/assess/progress/{job_id}", follow_redirects=False)
        if resp.status_code == 303:
            loc = resp.headers["location"]
            if "/assessments/" in loc:
                return loc.split("/assessments/")[1]
        time.sleep(0.1)
    raise TimeoutError(f"Assessment job {job_id} did not complete within {max_wait}s")


async def test_portal_llm_unavailable(client, _override_store):
    """Assessment completes successfully when LLM client is None."""
    report = _make_report_with_findings("no-llm-repo")

    with patch.object(assess_pipeline, "get_llm_client", return_value=None), \
         patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake")), \
         patch.object(assess_pipeline, "run_assessment", return_value=report), \
         patch.object(assess_pipeline, "_auto_create_infra_repo",
                      return_value="https://github.com/org/no-llm-repo-gitops"):
        resp = await client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/no-llm-repo", "criticality": "medium"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].split("/assess/progress/")[1]
        assessment_id = await _poll_assess_progress(client, job_id)

    assert assessment_id  # non-empty means it completed
