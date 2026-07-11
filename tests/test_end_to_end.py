from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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
from agentit.portal.store import AssessmentStore


def _make_store() -> AssessmentStore:
    return AssessmentStore(db_path=":memory:")


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
def _override_store():
    test_store = _make_store()
    with patch("agentit.portal.app.get_store", return_value=test_store):
        yield test_store


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------------
# 1. Full assess -> onboard -> view results flow
# ------------------------------------------------------------------


def test_assess_onboard_flow(client, _override_store):
    """POST /assess -> redirect to detail -> POST onboard -> redirect -> GET results shows manifests."""
    store = _override_store
    report = _make_report_with_findings("flow-repo")

    # Step 1: assess with mocked clone/run
    with patch("agentit.portal.app.clone_repo", return_value=Path("/tmp/fake")), \
         patch("agentit.portal.app.run_assessment", return_value=report):
        resp = client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/flow-repo", "criticality": "high"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/assessments/")
    assessment_id = location.split("/assessments/")[1]

    # Step 2: onboard
    resp = client.post(f"/assessments/{assessment_id}/onboard", follow_redirects=False)
    assert resp.status_code == 303
    assert f"/assessments/{assessment_id}/onboard-results" in resp.headers["location"]

    # Step 3: view results — manifests are shown
    resp = client.get(f"/assessments/{assessment_id}/onboard-results")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "observability" in resp.text


# ------------------------------------------------------------------
# 2. Onboard generates files for all dimensions
# ------------------------------------------------------------------


def test_onboard_generates_files_for_all_dimensions(client, _override_store):
    """Findings in security, observability, cicd, compliance -> manifests cover all four."""
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)

    resp = client.get(f"/api/assessments/{aid}/manifests")
    assert resp.status_code == 200
    data = resp.json()
    categories = {f["category"] for f in data}
    assert "security" in categories
    assert "observability" in categories
    assert "cicd" in categories
    assert "compliance" in categories


# ------------------------------------------------------------------
# 3. Onboard creates a gate
# ------------------------------------------------------------------


def test_onboard_does_not_auto_create_gate(client, _override_store):
    """POST onboard should NOT auto-create a gate — gates are only for risky actions."""
    store = _override_store
    report = _make_report_with_findings("gate-repo")
    aid = store.save(report)

    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)

    pending = store.list_gates(status="pending")
    gate_aids = [g["assessment_id"] for g in pending]
    assert aid not in gate_aids


# ------------------------------------------------------------------
# 4. Webhook onboard full flow
# ------------------------------------------------------------------


def test_webhook_onboard_full_flow(client, _override_store):
    """POST /api/webhook/onboard with correlationId -> 200 with files_generated count."""
    store = _override_store
    report = _make_report_with_findings("webhook-e2e")
    aid = store.save(report)

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
    with patch("agentit.portal.app._run_onboarding", return_value=(fake_files, fake_summary)):
        resp = client.post(
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


def test_assess_error_shows_form(client):
    """When run_assessment raises, POST /assess returns 400 with the assess form and error."""
    with patch("agentit.portal.app.clone_repo", return_value=Path("/tmp/fake")), \
         patch("agentit.portal.app.run_assessment", side_effect=RuntimeError("clone failed: repo not found")):
        resp = client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/bad-repo", "criticality": "medium"},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert "<form" in resp.text
    assert "clone failed" in resp.text


# ------------------------------------------------------------------
# 6. Re-assess from dashboard (same repo_url, new assessment)
# ------------------------------------------------------------------


def test_reassess_from_dashboard(client, _override_store):
    """Save a report, POST /assess with the same repo_url -> redirect to a new assessment."""
    store = _override_store
    original = _make_report_with_findings("reassess-repo")
    original_aid = store.save(original)

    new_report = _make_report_with_findings("reassess-repo")
    new_report.assessed_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    with patch("agentit.portal.app.clone_repo", return_value=Path("/tmp/fake")), \
         patch("agentit.portal.app.run_assessment", return_value=new_report):
        resp = client.post(
            "/assess",
            data={"repo_url": "https://github.com/org/reassess-repo", "criticality": "high"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    location = resp.headers["location"]
    new_aid = location.split("/assessments/")[1]
    # New assessment created, different from the original
    assert new_aid != original_aid

    # Both assessments exist
    assert store.get(original_aid) is not None
    assert store.get(new_aid) is not None
