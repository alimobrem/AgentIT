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
from agentit.platform_context import PlatformContext
from agentit.portal.app import app, get_store
from agentit.portal.services import assess_pipeline
from conftest import make_store, prime_csrf

# Discovery returning an empty context (no k8s_version, no available_kinds)
# is FleetOrchestrator.run()'s "discovery never actually connected" signal,
# which makes it skip the has_api() gate entirely (platform=None) --
# matching the ungated generation these tests assert on, regardless of
# whatever cluster happens to be reachable when the suite runs (e.g. a
# real, RBAC-restricted in-cluster ServiceAccount in CI vs. no cluster at
# all locally). See FleetOrchestrator.run() for the exact fallback logic.
_NO_CLUSTER = PlatformContext()


async def _poll_assess_progress(client, job_id: str, max_wait: float = 5.0) -> str:
    """Poll /assess/progress/{job_id} until it redirects to /assessments/{id}.
    Returns the assessment_id."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        resp = await client.get(f"/assess/progress/{job_id}", follow_redirects=False)
        if resp.status_code == 303:
            loc = resp.headers["location"]
            if "/assessments/" in loc:
                return loc.split("/assessments/")[1]
        time.sleep(0.1)
    raise TimeoutError(f"Assessment job {job_id} did not complete within {max_wait}s")


def _make_report_with_findings(repo_name: str = "e2e-repo") -> AssessmentReport:
    """Report with score ~30 and findings across security, observability, cicd, compliance."""
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
                    Finding(category="network security", severity=Severity.high,
                            description="No network policy", recommendation="Add NetworkPolicy"),
                    Finding(category="container security", severity=Severity.high,
                            description="No Containerfile found", recommendation="Add Containerfile"),
                    Finding(category="resource limits", severity=Severity.medium,
                            description="No resource limits", recommendation="Add resource limits"),
                ],
            ),
            DimensionScore(
                dimension="observability",
                score=20,
                max_score=100,
                findings=[
                    Finding(category="metrics endpoint", severity=Severity.medium,
                            description="No metrics endpoint", recommendation="Add /metrics"),
                    Finding(category="tracing", severity=Severity.medium,
                            description="No distributed tracing", recommendation="Add OpenTelemetry"),
                ],
            ),
            DimensionScore(
                dimension="cicd",
                score=25,
                max_score=100,
                findings=[
                    Finding(category="pipeline cicd", severity=Severity.high,
                            description="No CI/CD pipeline", recommendation="Add Tekton pipeline"),
                    Finding(category="gitops deployment", severity=Severity.medium,
                            description="No GitOps deployment", recommendation="Add ArgoCD"),
                ],
            ),
            DimensionScore(
                dimension="compliance",
                score=35,
                max_score=100,
                findings=[
                    Finding(category="policy compliance", severity=Severity.medium,
                            description="No admission policies", recommendation="Add Kyverno policies"),
                    Finding(category="sbom supply chain", severity=Severity.medium,
                            description="No SBOM generation", recommendation="Add SBOM tooling"),
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
# 1. Full assess -> onboard -> view results flow
# ------------------------------------------------------------------


async def test_assess_onboard_flow(client, _override_store):
    """POST /assess -> async progress -> redirect to detail -> POST onboard -> redirect -> GET results shows manifests."""
    store = _override_store
    report = _make_report_with_findings("flow-repo")

    # Step 1: assess with mocked clone/run — now async via background thread.
    # continue_onboard=0 explicitly opts out of the now-default assess->
    # onboard chaining (docs/onboarding-loop-vision-gap-analysis.md Phase 0
    # item 5) so this test can keep exercising the separate, manual
    # "Step 2: onboard" POST below in isolation.
    with patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake")), \
         patch.object(assess_pipeline, "run_assessment", return_value=report), \
         patch.object(assess_pipeline, "_auto_create_infra_repo",
                      return_value="https://github.com/org/flow-repo-gitops"):
        resp = await client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/flow-repo", "criticality": "high", "continue_onboard": "0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "/assess/progress/" in location
        job_id = location.split("/assess/progress/")[1]
        assessment_id = await _poll_assess_progress(client, job_id)

    # Step 2: onboard — pin platform discovery so this test's outcome
    # doesn't depend on whatever cluster happens to be reachable at test
    # time (see FleetOrchestrator.run()'s platform=None fallback).
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        resp = await client.post(f"/assessments/{assessment_id}/onboard", follow_redirects=False)
    assert resp.status_code == 303
    # Onboard now runs as a background job with a real-time progress page
    # (docs/ux-design-requirements.md checklist #6/#8) -- the immediate
    # redirect target is the progress page, which itself redirects on to
    # onboard-results once the (by-now-completed, per TestClient's
    # background-task-before-return semantics) job is done.
    assert f"/assessments/{assessment_id}/onboard/progress/" in resp.headers["location"]
    progress_resp = await client.get(resp.headers["location"], follow_redirects=False)
    assert progress_resp.status_code == 303
    assert f"/assessments/{assessment_id}/onboard-results" in progress_resp.headers["location"]

    # Step 3: view results — manifests are shown
    resp = await client.get(f"/assessments/{assessment_id}/onboard-results")
    assert resp.status_code == 200
    # security/observability are now skill-only domains (see
    # docs/agent-removal-readiness.md) -- generated manifests are grouped
    # under the "skills" category instead of one category per domain.
    assert "skills" in resp.text


# ------------------------------------------------------------------
# 2. Onboard generates files for all dimensions
# ------------------------------------------------------------------


async def test_onboard_generates_files_for_all_dimensions(client, _override_store):
    """Findings in security, observability, cicd, compliance -> manifests cover all four."""
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    # Pin platform discovery — see comment in test_assess_onboard_flow.
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)

    resp = await client.get(f"/api/assessments/{aid}/manifests")
    assert resp.status_code == 200
    data = resp.json()
    # security/observability/cicd/compliance are now skill-only domains
    # (see docs/agent-removal-readiness.md) -- all four still produce
    # output, but grouped under the shared "skills" category.
    categories = {f["category"] for f in data}
    assert "skills" in categories
    paths = {f["path"] for f in data}
    assert any("network-policy" in p for p in paths), paths
    assert any("service-monitor" in p for p in paths), paths
    assert any("tekton-pipeline" in p for p in paths), paths
    assert any("kyverno" in p for p in paths), paths


# ------------------------------------------------------------------
# 4. Webhook onboard full flow
# ------------------------------------------------------------------


async def test_webhook_onboard_full_flow(client, _override_store):
    """POST /api/webhook/onboard with correlationId -> 200 with files_generated count."""
    store = _override_store
    report = _make_report_with_findings("webhook-e2e")
    aid = await store.save(report)

    fake_files = [
        {"category": "security", "path": "netpol.yaml", "description": "netpol.yaml",
         "content": "kind: NetworkPolicy"},
        {"category": "observability", "path": "servicemonitor.yaml", "description": "servicemonitor.yaml",
         "content": "kind: ServiceMonitor"},
    ]
    fake_summary = {
        "agents": [], "conflicts": [], "recommendation": "",
        "auto_approve": False, "gates": [],
    }
    with patch("agentit.portal.routes.webhooks.run_onboarding", return_value=(fake_files, fake_summary)):
        resp = await client.post(
            "/api/webhook/onboard",
            json={"correlationId": aid},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["assessment_id"] == aid
    assert data["files_generated"] == 2
    assert "security" in data["categories"]
    assert "observability" in data["categories"]


# ------------------------------------------------------------------
# 5. Assess error shows form with error message
# ------------------------------------------------------------------


async def test_assess_error_shows_progress(client):
    """When run_assessment raises, the progress page shows the error."""
    with patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake")), \
         patch.object(assess_pipeline, "run_assessment", side_effect=RuntimeError("clone failed: repo not found")), \
         patch.object(assess_pipeline, "_auto_create_infra_repo",
                      return_value="https://github.com/org/bad-repo-gitops"):
        resp = await client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/bad-repo", "criticality": "medium"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].split("/assess/progress/")[1]
        # Wait for background thread to fail
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.3)
            resp = await client.get(f"/assess/progress/{job_id}")
            if "Could not clone" in resp.text or "clone failed" in resp.text:
                break
    assert "clone" in resp.text.lower()


# ------------------------------------------------------------------
# 6. Re-assess from dashboard (same repo_url, new assessment)
# ------------------------------------------------------------------


async def test_reassess_from_dashboard(client, _override_store):
    """Save a report, POST /assess with the same repo_url -> async progress -> redirect to a new assessment."""
    store = _override_store
    original = _make_report_with_findings("reassess-repo")
    original_aid = await store.save(original)

    new_report = _make_report_with_findings("reassess-repo")
    new_report.assessed_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    # continue_onboard=0 explicitly opts out of the now-default assess->
    # onboard chaining (docs/onboarding-loop-vision-gap-analysis.md Phase 0
    # item 5) -- this test only cares about the new-vs-original assessment
    # identity, not onboarding.
    with patch.object(assess_pipeline, "clone_repo", return_value=Path("/tmp/fake")), \
         patch.object(assess_pipeline, "run_assessment", return_value=new_report), \
         patch.object(assess_pipeline, "_auto_create_infra_repo",
                      return_value="https://github.com/org/reassess-repo-gitops"):
        resp = await client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/reassess-repo", "criticality": "high", "continue_onboard": "0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        job_id = resp.headers["location"].split("/assess/progress/")[1]
        new_aid = await _poll_assess_progress(client, job_id)

    # New assessment created, different from the original
    assert new_aid != original_aid

    # Both assessments exist
    assert await store.get(original_aid) is not None
    assert await store.get(new_aid) is not None
