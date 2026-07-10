from __future__ import annotations

import io
import tempfile
import zipfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    RemediationItem,
    Severity,
    StackInfo,
)
from agentit.portal.app import app, get_store
from agentit.portal.store import AssessmentStore


def _make_store() -> AssessmentStore:
    """Create an in-memory store for testing."""
    return AssessmentStore(db_path=":memory:")


def _make_report(repo_name: str = "test-repo") -> AssessmentReport:
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
                score=45,
                max_score=100,
                findings=[
                    Finding(
                        category="secrets",
                        severity=Severity.high,
                        description="No secret scanning configured",
                        recommendation="Add secret scanning",
                    ),
                ],
            ),
            DimensionScore(
                dimension="observability",
                score=60,
                max_score=100,
                findings=[],
            ),
        ],
        criticality="medium",
        summary="Test summary",
        remediation_plan=[
            RemediationItem(
                priority=1,
                dimension="security",
                description="Add secret scanning",
                estimated_effort="1 agent-hour",
                agent_responsible="Security Hardening Agent",
            ),
        ],
    )


@pytest.fixture(autouse=True)
def _override_store():
    """Patch get_store so every test gets a fresh in-memory DB."""
    test_store = _make_store()
    with patch("agentit.portal.app.get_store", return_value=test_store):
        yield test_store


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No assessments" in resp.text


def test_assess_form(client):
    resp = client.get("/assess")
    assert resp.status_code == 200
    assert "<form" in resp.text
    assert "repo_url" in resp.text


def test_api_list_empty(client):
    resp = client.get("/api/assessments")
    assert resp.status_code == 200
    assert resp.json() == []


def test_save_and_retrieve(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)

    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "test-repo" in resp.text


def test_api_roundtrip(client, _override_store):
    store = _override_store
    report = _make_report("my-service")
    aid = store.save(report)

    resp = client.get(f"/api/assessments/{aid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo_name"] == "my-service"
    assert "scores" in data


def test_assessment_not_found(client):
    resp = client.get("/assessments/nonexistent")
    assert resp.status_code == 404


def test_api_detail_not_found(client):
    resp = client.get("/api/assessments/nonexistent")
    assert resp.status_code == 404


# ------------------------------------------------------------------
# Onboarding tests
# ------------------------------------------------------------------


def _make_report_with_findings(repo_name: str = "onboard-repo") -> AssessmentReport:
    """Report with findings that trigger all agent generators."""
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
                    Finding(
                        category="resource limits",
                        severity=Severity.medium,
                        description="No resource limits",
                        recommendation="Add resource limits",
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
                    Finding(
                        category="tracing",
                        severity=Severity.medium,
                        description="No distributed tracing",
                        recommendation="Add OpenTelemetry",
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
                    Finding(
                        category="gitops deployment",
                        severity=Severity.medium,
                        description="No GitOps deployment",
                        recommendation="Add ArgoCD",
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
                    Finding(
                        category="sbom supply chain",
                        severity=Severity.medium,
                        description="No SBOM generation",
                        recommendation="Add SBOM tooling",
                    ),
                    Finding(
                        category="audit logging",
                        severity=Severity.medium,
                        description="No audit policy",
                        recommendation="Add audit policy",
                    ),
                ],
            ),
        ],
        criticality="high",
        summary="Needs onboarding",
        remediation_plan=[],
    )


def test_onboard_creates_results(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    resp = client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    assert resp.status_code == 303
    assert f"/assessments/{aid}/onboard-results" in resp.headers["location"]


def test_onboard_results_page(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "observability" in resp.text
    assert "compliance" in resp.text
    assert "Download ZIP" in resp.text
    assert "Create GitHub PR" in resp.text
    assert "Apply to Cluster" in resp.text
    assert "Dry Run" in resp.text


def test_api_manifests(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = client.get(f"/api/assessments/{aid}/manifests")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    categories = {f["category"] for f in data}
    assert "security" in categories
    assert "observability" in categories
    assert "compliance" in categories


def test_api_manifests_not_found(client):
    resp = client.get("/api/assessments/nonexistent/manifests")
    assert resp.status_code == 404


def test_onboard_not_found(client):
    resp = client.post("/assessments/nonexistent/onboard", follow_redirects=False)
    assert resp.status_code == 404


def test_onboard_results_no_onboarding(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)

    resp = client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 404


def test_download_manifests_zip(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    # Run onboarding to populate files
    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)

    resp = client.get(f"/api/assessments/{aid}/manifests/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "onboard-repo-onboarding.zip" in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) > 0

    # Every entry should be under a known category directory
    categories = {n.split("/")[0] for n in names}
    assert "security" in categories
    assert "observability" in categories
    assert "compliance" in categories

    # Verify files are readable and non-empty
    for name in names:
        assert len(zf.read(name)) > 0
    zf.close()


def test_download_manifests_not_found(client):
    resp = client.get("/api/assessments/nonexistent/manifests/download")
    assert resp.status_code == 404


def test_download_manifests_no_onboarding(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)

    resp = client.get(f"/api/assessments/{aid}/manifests/download")
    assert resp.status_code == 404


def test_assessment_detail_has_onboard_button(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)

    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/onboard" in resp.text
    assert "Onboard" in resp.text
