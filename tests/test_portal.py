from __future__ import annotations

import tempfile
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
