from __future__ import annotations

import io
import tempfile
import zipfile
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
    RemediationItem,
    Severity,
    StackInfo,
)
from agentit.portal.app import app, get_store
from conftest import make_store


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
    test_store = make_store()
    with patch("agentit.portal.app.get_store", return_value=test_store):
        yield test_store


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Assess your first app" in resp.text


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
    assert "Download" in resp.text
    assert "Create PR" in resp.text
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


# ------------------------------------------------------------------
# Events & Webhook tests
# ------------------------------------------------------------------


def test_events_page_renders(client, _override_store):
    store = _override_store
    store.log_event("test-agent", "scan", "my-app", "high", "Found vuln")
    resp = client.get("/events")
    assert resp.status_code == 200
    assert "Agent Activity Feed" in resp.text
    assert "test-agent" in resp.text
    assert "Found vuln" in resp.text


def test_webhook_triggers_assessment(client, _override_store):
    report = _make_report("webhook-repo")
    with patch("agentit.portal.app.clone_repo") as mock_clone, \
         patch("agentit.portal.app.run_assessment", return_value=report):
        mock_clone.return_value = Path("/tmp/fake")
        resp = client.post(
            "/api/webhook/assess",
            json={"repo_url": "https://github.com/org/webhook-repo", "criticality": "high"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "assessment_id" in data
    assert "overall_score" in data


def test_webhook_missing_repo_url(client):
    resp = client.post("/api/webhook/assess", json={"criticality": "high"})
    assert resp.status_code == 400


def test_webhook_onboard_triggers_onboarding(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    fake_files = [{"category": "security", "path": "netpol.yaml", "content": "kind: NetworkPolicy", "description": "netpol"}]
    fake_summary = {"agents": [], "conflicts": [], "recommendation": "READY", "auto_approve": False, "gates": []}
    with patch("agentit.portal.app._run_onboarding", return_value=(fake_files, fake_summary)):
        resp = client.post(
            "/api/webhook/onboard",
            json={"correlationId": aid},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["assessment_id"] == aid
    assert data["files_generated"] == 1
    assert "security" in data["categories"]


def test_webhook_onboard_missing_assessment_id(client):
    resp = client.post("/api/webhook/onboard", json={"eventId": "evt-123"})
    assert resp.status_code == 400


def test_webhook_onboard_assessment_not_found(client):
    resp = client.post(
        "/api/webhook/onboard",
        json={"correlationId": "nonexistent-id"},
    )
    assert resp.status_code == 404


def test_api_events_returns_json(client, _override_store):
    store = _override_store
    store.log_event("agent-a", "deploy", "app-x", "info", "Deployed v2")
    store.log_event("agent-b", "scan", "app-y", "medium", "Scan done")
    resp = client.get("/api/events")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 2


def test_api_events_filter_target_app(client, _override_store):
    store = _override_store
    store.log_event("a", "x", "app-1", "info", "e1")
    store.log_event("b", "y", "app-2", "info", "e2")
    resp = client.get("/api/events?target_app=app-1")
    assert resp.status_code == 200
    data = resp.json()
    assert all(e["target_app"] == "app-1" for e in data)


# ------------------------------------------------------------------
# Gate queue tests
# ------------------------------------------------------------------


def test_gates_page_empty(client):
    resp = client.get("/gates")
    assert resp.status_code == 200
    assert "No pending gates" in resp.text


def test_gates_page_with_pending(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    store.create_gate(aid, "deploy", "Approve deployment of test-repo")

    resp = client.get("/gates")
    assert resp.status_code == 200
    assert "Approve deployment of test-repo" in resp.text
    assert "Approve" in resp.text
    assert "Reject" in resp.text


def test_resolve_gate_approve(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    gate_id = store.create_gate(aid, "deploy", "Approve deployment of test-repo")

    resp = client.post(
        f"/gates/{gate_id}/resolve",
        data={"status": "approved", "resolved_by": "tester"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/gates"

    pending = store.list_gates(status="pending")
    assert len(pending) == 0
    approved = store.list_gates(status="approved")
    assert len(approved) == 1
    assert approved[0]["resolved_by"] == "tester"


# ------------------------------------------------------------------
# Fleet dashboard tests
# ------------------------------------------------------------------


def _make_report_scored(repo_name: str, score: int, criticality: str = "medium") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", version="3.12", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=["pip"],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[], auth_mechanism=None,
        ),
        scores=[
            DimensionScore(
                dimension="security", score=score, max_score=100,
                findings=[
                    Finding(
                        category="secrets", severity=Severity.critical,
                        description="Crit finding", recommendation="Fix it",
                    ),
                ],
            ),
        ],
        criticality=criticality,
        summary="Test",
        remediation_plan=[],
    )


def test_fleet_dashboard_shows_portfolio_summary(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("alpha-svc", 80, "low"))
    store.save(_make_report_scored("beta-svc", 30, "critical"))
    store.save(_make_report_scored("gamma-svc", 55, "medium"))

    resp = client.get("/")
    assert resp.status_code == 200
    assert "alpha-svc" in resp.text
    assert "beta-svc" in resp.text
    assert "gamma-svc" in resp.text
    assert "Assess New Repo" in resp.text


def test_fleet_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Assess your first app" in resp.text


def test_fleet_redirects_to_home(client):
    resp = client.get("/fleet", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/"


def test_api_fleet_returns_json(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("svc-a", 75))
    store.save(_make_report_scored("svc-b", 40))

    resp = client.get("/api/fleet")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    names = {r["repo_name"] for r in data}
    assert names == {"svc-a", "svc-b"}
    for r in data:
        assert "latest_score" in r
        assert "delta" in r
        assert "critical_count" in r
        assert "assessment_count" in r
        assert "last_assessed" in r


def test_api_fleet_trend_with_multiple_assessments(client, _override_store):
    store = _override_store
    r1 = _make_report_scored("trending", 40)
    store.save(r1)
    r2 = _make_report_scored("trending", 60)
    r2.assessed_at = datetime(2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    store.save(r2)

    resp = client.get("/api/fleet")
    data = resp.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["latest_score"] == 60
    assert entry["previous_score"] == 40
    assert entry["delta"] == 20.0
    assert entry["assessment_count"] == 2


def test_dashboard_shows_portfolio_summary(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("app-one", 90))
    store.save(_make_report_scored("app-two", 60))

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Apps" in resp.text
    assert "Avg Score" in resp.text
    assert "Critical" in resp.text


def test_base_nav_has_assess_link(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/assess"' in resp.text


# ------------------------------------------------------------------
# UI design system tests
# ------------------------------------------------------------------


def test_base_has_htmx_script(client):
    resp = client.get("/")
    assert "htmx.org@2.0.4" in resp.text
    assert 'integrity="sha384-' in resp.text


def test_base_has_alpinejs_script(client):
    resp = client.get("/")
    assert "alpinejs@3" in resp.text
    assert 'crossorigin="anonymous"' in resp.text


def test_base_has_hx_boost(client):
    resp = client.get("/")
    assert 'hx-boost="true"' in resp.text


def test_base_has_css_variables(client):
    resp = client.get("/")
    assert "--color-bg:" in resp.text
    assert "--color-accent:" in resp.text
    assert "--color-surface:" in resp.text
    assert "--radius-md:" in resp.text
    assert "--space-" in resp.text


def test_no_inline_styles_dashboard(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("styled-app", 70))
    resp = client.get("/")
    html = resp.text
    lines = html.split("\n")
    for i, line in enumerate(lines, 1):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found on line {i}: {line.strip()}"


def test_no_inline_styles_fleet(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("fleet-app", 50))
    resp = client.get("/fleet")
    html = resp.text
    lines = html.split("\n")
    for i, line in enumerate(lines, 1):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found on line {i}: {line.strip()}"


def test_no_inline_styles_assess_form(client):
    resp = client.get("/assess")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_assessment_detail(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    resp = client.get(f"/assessments/{aid}")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_events(client):
    resp = client.get("/events")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_gates(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    store.create_gate(aid, "deploy", "Test gate")
    resp = client.get("/gates")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_onboard_results(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)
    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = client.get(f"/assessments/{aid}/onboard-results")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="width:' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_dashboard_uses_design_system_classes(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("css-app", 60))
    resp = client.get("/")
    assert "stat-grid" in resp.text
    assert "stat-card" in resp.text
    assert "stat-label" in resp.text
    assert "stat-value" in resp.text
    assert "card-grid" in resp.text
    assert "card-header" in resp.text
    assert "card-title" in resp.text
    assert "card-score" in resp.text
    assert "card-meta" in resp.text
    assert "card-footer" in resp.text


def test_fleet_uses_design_system_classes(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("fleet-css", 80))
    resp = client.get("/fleet")
    assert "stat-grid" in resp.text
    assert "row-border-" in resp.text
    assert "text-bold" in resp.text
    assert "btn btn-sm" in resp.text


def test_assessment_detail_uses_design_system_classes(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    resp = client.get(f"/assessments/{aid}")
    assert "score-hero" in resp.text
    assert "score-unit" in resp.text
    assert "section-title" in resp.text
    assert "dimension-row" in resp.text
    assert "dimension-label" in resp.text
    assert "dimension-bar" in resp.text
    assert "dimension-value" in resp.text
    assert "finding-list" in resp.text
    assert "btn-action" in resp.text


def test_assess_form_uses_design_system_classes(client):
    resp = client.get("/assess")
    assert "form-narrow" in resp.text
    assert "form-group" in resp.text
    assert "form-label" in resp.text
    assert "htmx-indicator" in resp.text


def test_gates_uses_design_system_classes(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    store.create_gate(aid, "deploy", "Gate test")
    resp = client.get("/gates")
    assert "gate-actions" in resp.text
    assert "btn-approve" in resp.text
    assert "btn-reject" in resp.text
    assert "section-title" in resp.text


def test_onboard_results_uses_design_system_classes(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)
    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = client.get(f"/assessments/{aid}/onboard-results")
    assert "manifest-card" in resp.text
    assert "manifest-title" in resp.text
    assert "manifest-desc" in resp.text
    assert "code-block" in resp.text
    assert "action-bar" in resp.text
    assert 'hx-boost="false"' in resp.text


def test_responsive_css_exists(client):
    resp = client.get("/")
    assert "@media (max-width: 768px)" in resp.text


# ── Agents page ────────────────────────────────────────────────────────


def test_agents_page_empty(client, _override_store):
    resp = client.get("/agents")
    assert resp.status_code == 200
    assert "Agent Registry" in resp.text
    assert "No agents registered" in resp.text


def test_agents_page_with_data(client, _override_store):
    store = _override_store
    store.register_agent("security", "hardening", "network,rbac")
    store.register_agent("observability", "monitoring")
    resp = client.get("/agents")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "observability" in resp.text
    assert "hardening" in resp.text


def test_api_agents(client, _override_store):
    store = _override_store
    store.register_agent("cicd", "deployment")
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert any(a["agent_name"] == "cicd" for a in data)


def test_agent_detail_page(client, _override_store):
    store = _override_store
    store.register_agent("security", "hardening", "network,rbac")
    store.log_event("security", "completed", "test-app", "info", "Generated 5 files")
    report = _make_report()
    aid = store.save(report)
    store.save_remediation(aid, "security", "Add NetworkPolicy")
    resp = client.get("/agents/security")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "hardening" in resp.text
    assert "Generated 5 files" in resp.text
    assert "Add NetworkPolicy" in resp.text


def test_agent_detail_not_found(client, _override_store):
    resp = client.get("/agents/nonexistent")
    assert resp.status_code == 404


def test_agents_page_links_to_detail(client, _override_store):
    store = _override_store
    store.register_agent("observability", "monitoring")
    resp = client.get("/agents")
    assert resp.status_code == 200
    assert 'href="/agents/observability"' in resp.text


# ── Remediations page ─────────────────────────────────────────────────


def test_remediations_page(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_remediation(aid, "security", "Fix RBAC")
    store.save_remediation(aid, "observability", "Add metrics")
    resp = client.get(f"/assessments/{aid}/remediations")
    assert resp.status_code == 200
    assert "Remediations" in resp.text
    assert "Fix RBAC" in resp.text
    assert "Add metrics" in resp.text


def test_remediations_page_empty(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.get(f"/assessments/{aid}/remediations")
    assert resp.status_code == 200
    assert "No remediations" in resp.text


def test_complete_remediation(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    rid = store.save_remediation(aid, "security", "Fix RBAC")
    resp = client.post(f"/assessments/{aid}/remediations/{rid}/complete", follow_redirects=False)
    assert resp.status_code == 303
    rems = store.list_remediations(aid)
    assert rems[0]["status"] == "completed"


def test_api_remediations(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_remediation(aid, "compliance", "Add SBOM")
    resp = client.get(f"/api/assessments/{aid}/remediations")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── SLOs page ──────────────────────────────────────────────────────────


def test_slos_page(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_slo(aid, "availability", 99.9)
    store.save_slo(aid, "error_rate", 0.1)
    resp = client.get(f"/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert "SLOs" in resp.text
    assert "availability" in resp.text
    assert "error_rate" in resp.text


def test_slos_page_empty(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.get(f"/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert "No SLOs defined" in resp.text


def test_api_slos(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_slo(aid, "latency_p99", 200.0)
    resp = client.get(f"/api/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── Nav bar ────────────────────────────────────────────────────────────


def test_nav_includes_agents_link(client):
    resp = client.get("/")
    assert 'href="/agents"' in resp.text
    assert "Agents" in resp.text


# ── Assessment detail shows remediation/SLO buttons ────────────────────


def test_assessment_detail_shows_remediation_button(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_remediation(aid, "security", "Fix it")
    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/remediations" in resp.text
    assert "Remediations (1)" in resp.text


def test_assessment_detail_hides_buttons_when_empty(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "Remediations" not in resp.text or "Remediations (0)" not in resp.text


# ── SLO add form ───────────────────────────────────────────────────────


def test_add_slo_via_form(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.post(
        f"/assessments/{aid}/slos/add",
        data={"metric_name": "availability", "target_value": "99.9"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    slos = store.list_slos(aid)
    assert len(slos) == 1
    assert slos[0]["metric_name"] == "availability"
    assert slos[0]["target_value"] == 99.9


def test_slos_page_shows_add_form(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.get(f"/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert "Add SLO" in resp.text
    assert "metric_name" in resp.text
    assert "target_value" in resp.text


# ── Onboarding history ─────────────────────────────────────────────────


def test_onboarding_history_empty(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.get(f"/assessments/{aid}/onboarding-history")
    assert resp.status_code == 200
    assert "No onboarding runs" in resp.text


def test_onboarding_history_with_data(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_onboarding(aid, [
        {"category": "security", "path": "rbac.yaml", "content": "kind: Role", "description": "rbac"},
    ], orchestration={"recommendation": "READY FOR REVIEW", "auto_approve": False})
    resp = client.get(f"/assessments/{aid}/onboarding-history")
    assert resp.status_code == 200
    assert "READY FOR REVIEW" in resp.text
    assert "1" in resp.text  # file count


def test_assessment_detail_shows_history_button(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_onboarding(aid, [{"category": "c", "path": "f.yaml", "content": "x", "description": "d"}])
    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/onboarding-history" in resp.text
    assert "History (1)" in resp.text


# ── Settings page ──────────────────────────────────────────────────────


def test_settings_page_default(client, _override_store):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    assert "Auto-Mode" in resp.text
    assert "OFF" in resp.text


def test_toggle_auto_mode_on(client, _override_store):
    store = _override_store
    resp = client.post("/settings/auto-mode", data={"value": "true"}, follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_setting("auto_mode") == "true"


def test_toggle_auto_mode_off(client, _override_store):
    store = _override_store
    store.set_setting("auto_mode", "true")
    resp = client.post("/settings/auto-mode", data={"value": "false"}, follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_setting("auto_mode") == "false"


def test_settings_page_shows_on_when_enabled(client, _override_store):
    store = _override_store
    store.set_setting("auto_mode", "true")
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "ON" in resp.text
    assert "Disable Auto-Mode" in resp.text


def test_settings_nav_link(client):
    resp = client.get("/")
    assert 'href="/settings"' in resp.text


# ── Schedules page ─────────────────────────────────────────────────────


def test_schedules_page_empty(client, _override_store):
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert "Scheduled Operations" in resp.text


def test_schedules_page_shows_watchers(client, _override_store):
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert "vuln-watcher" in resp.text
    assert "slo-tracker" in resp.text
    assert "drift-detector" in resp.text


def test_schedules_nav_link(client):
    resp = client.get("/")
    assert 'href="/schedules"' in resp.text


def test_update_schedule(client, _override_store):
    store = _override_store
    resp = client.post("/schedules/update", data={
        "app_name": "test-app",
        "job_key": "compliance",
        "schedule": "0 6 1 * *",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_setting("schedule:test-app:compliance") == "0 6 1 * *"


def test_toggle_schedule(client, _override_store):
    store = _override_store
    resp = client.post("/schedules/toggle", data={
        "app_name": "test-app",
        "job_key": "chaos",
        "enabled": "false",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_setting("schedule:test-app:chaos:enabled") == "false"


# ── All pages accessible ──────────────────────────────────────────────


def test_all_pages_return_200(client, _override_store):
    """Smoke test: every page returns 200."""
    store = _override_store
    aid = store.save(_make_report())
    store.register_agent("security", "hardening")

    pages = [
        "/",
        "/assess",
        "/events",
        "/gates",
        "/agents",
        "/schedules",
        "/settings",
        f"/assessments/{aid}",
    ]
    for page in pages:
        resp = client.get(page)
        assert resp.status_code == 200, f"{page} returned {resp.status_code}"
