from __future__ import annotations

import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from agentit.portal.app import app, get_store, _get_trusted_base_url
from conftest import make_store, prime_csrf


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
    """Patch get_store so every test gets a fresh in-memory DB.

    Also patches image_builder.build_app_image: onboarding a report whose
    findings include a missing Containerfile causes HardeningAgent to
    generate one, which flips `has_containerfile` in app.py and triggers a
    REAL `oc apply` PipelineRun via subprocess — against whatever cluster
    the local kubeconfig happens to point to. Without this patch, running
    this suite with an active `oc login` silently floods a real cluster.
    """
    test_store = make_store()
    with patch("agentit.portal.app.get_store", return_value=test_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=test_store), \
         patch("agentit.portal.routes.health.get_store", return_value=test_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=test_store), \
         patch("agentit.image_builder.build_app_image",
               return_value={"image_ref": "test/image:test", "run_name": "test-run", "status": "skipped-in-tests"}):
        yield test_store


@pytest.fixture
def client():
    c = TestClient(app)
    prime_csrf(c)
    return c


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


def test_trusted_base_url_ignores_forged_host_header(client, _override_store):
    """A forged Host header must not affect the webhook URL registered with GitHub.

    Regression test for the reflected-Host-header issue: with no
    AGENTIT_EXTERNAL_URL override and no reachable cluster Route, the request's
    Host header used to be trusted outright.
    """
    store = _override_store
    report = _make_report()
    aid = store.save(report)

    fake_routes = [
        {"metadata": {"labels": {"app.kubernetes.io/name": "agentit"}},
         "spec": {"host": "agentit.apps.cluster.example.com"}},
    ]
    with patch("agentit.portal.app._run_onboarding", return_value=([], {})), \
         patch("agentit.portal.github_pr.ensure_webhook") as mock_ensure_webhook, \
         patch("agentit.kube.list_custom_resources", return_value=fake_routes), \
         patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
        mock_ensure_webhook.return_value = {"created": True}
        client.post(
            f"/assessments/{aid}/onboard",
            follow_redirects=False,
            headers={"Host": "evil.example.com"},
        )
        assert mock_ensure_webhook.called
        registered_url = mock_ensure_webhook.call_args[0][1]
        assert "evil.example.com" not in registered_url
        assert registered_url.startswith("https://agentit.apps.cluster.example.com")


def test_get_trusted_base_url_env_override():
    """AGENTIT_EXTERNAL_URL, when set, wins over both the Route lookup and the request."""
    request = MagicMock()
    request.base_url = "http://untrusted.example.com/"
    with patch.dict(os.environ, {"AGENTIT_EXTERNAL_URL": "https://agentit.apps.cluster.example.com/"}):
        assert _get_trusted_base_url(request) == "https://agentit.apps.cluster.example.com"


def test_get_trusted_base_url_uses_own_route():
    """With no override, the app's own OpenShift Route host is used, not the request's Host."""
    request = MagicMock()
    request.base_url = "http://untrusted.example.com/"
    fake_routes = [
        {"metadata": {"labels": {"app.kubernetes.io/name": "agentit"}},
         "spec": {"host": "agentit.apps.cluster.example.com"}},
    ]
    with patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, clear=False), \
         patch("agentit.kube.list_custom_resources", return_value=fake_routes):
        os.environ.pop("AGENTIT_EXTERNAL_URL", None)
        assert _get_trusted_base_url(request) == "https://agentit.apps.cluster.example.com"


def test_get_trusted_base_url_falls_back_to_request_when_not_in_cluster():
    """Outside a cluster (no KUBERNETES_SERVICE_HOST, e.g. local dev/tests), skip
    the Route lookup entirely and fall back to the request -- this also avoids
    every call attempting a real (possibly slow/unreachable) kubeconfig-based
    connection from a developer's machine."""
    request = MagicMock()
    request.base_url = "http://localhost:8080/"
    with patch.dict(os.environ, {}, clear=False), \
         patch("agentit.kube.list_custom_resources") as mock_list:
        os.environ.pop("AGENTIT_EXTERNAL_URL", None)
        os.environ.pop("KUBERNETES_SERVICE_HOST", None)
        assert _get_trusted_base_url(request) == "http://localhost:8080"
        mock_list.assert_not_called()


def test_get_trusted_base_url_falls_back_to_request_when_route_unresolvable():
    """In-cluster but the Route lookup itself fails (RBAC, API error, etc) --
    still fall back to the request rather than raising."""
    request = MagicMock()
    request.base_url = "http://localhost:8080/"
    with patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, clear=False), \
         patch("agentit.kube.list_custom_resources", side_effect=Exception("no cluster")):
        os.environ.pop("AGENTIT_EXTERNAL_URL", None)
        assert _get_trusted_base_url(request) == "http://localhost:8080"


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


def test_verify_properties_not_found(client):
    resp = client.get("/api/assessments/nonexistent/verify")
    assert resp.status_code == 404


def test_verify_properties_no_onboarding(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)

    resp = client.get(f"/api/assessments/{aid}/verify")
    assert resp.status_code == 404


def test_verify_properties_runs_against_generated_files(client, _override_store):
    """Regression: this endpoint used to call verify_all_properties(repo_name,
    repo_name) -- two strings -- while verify_all_properties() expects a
    list[GeneratedFile]. Any real call would raise a TypeError."""
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)

    store.save_onboarding(aid, [
        {
            "category": "security",
            "path": "netpol.yaml",
            "description": "netpol.yaml",
            "content": (
                "apiVersion: networking.k8s.io/v1\n"
                "kind: NetworkPolicy\n"
                "metadata:\n  name: x\n"
                "spec:\n  podSelector: {}\n  policyTypes:\n    - Ingress\n"
            ),
        },
    ])

    resp = client.get(f"/api/assessments/{aid}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == report.repo_name
    assert "results" in body
    properties = {r["property"] for r in body["results"]}
    assert "Network Isolation" in properties


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


def test_events_page_filters_by_correlation_id(client, _override_store):
    store = _override_store
    store.log_event("orchestrator", "completed", "app-a", "info", "chain event", correlation_id="chain-xyz")
    store.log_event("orchestrator", "completed", "app-b", "info", "other event", correlation_id="chain-other")
    resp = client.get("/events?correlation_id=chain-xyz")
    assert resp.status_code == 200
    assert "chain event" in resp.text
    assert "other event" not in resp.text


def test_dlq_retry_republishes_and_redirects(client, _override_store):
    store = _override_store
    eid = store.log_event(
        "event-consumer", "dead-letter", "app", "error", "Dead-lettered",
        details={
            "original_topic": "agentit-events",
            "original_message": {"agentId": "x", "action": "tick", "result": {"summary": "", "details": {}}},
            "error": "boom",
        },
    )
    resp = client.post(f"/events/dlq/{eid}/retry", follow_redirects=False)
    assert resp.status_code == 303
    assert "success" in resp.headers["location"]
    assert store.list_dlq_messages() == []


def test_insights_page_shows_fleet_wide_feedback(client, _override_store):
    """Regression: insights_page used get_feedback_for_app(""), which filters
    on app_name = '' and always returns nothing -- get_all_feedback fixes it."""
    store = _override_store
    store.record_feedback("app-a", "security", "network-policy", "rejected", human_reason="not needed here")
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert "not needed here" in resp.text


def test_insights_page_shows_check_compliance(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_check_results(aid, [
        {"check_name": "has-network-policy", "dimension": "security", "passed": True},
    ])
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert "Fleet-Wide Check Compliance" in resp.text
    assert "has-network-policy" in resp.text


def test_webhook_triggers_assessment(client, _override_store):
    report = _make_report("webhook-repo")
    with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=report):
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
    with patch("agentit.portal.routes.webhooks.run_onboarding", return_value=(fake_files, fake_summary)):
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


def test_fleet_has_assess_modal(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="assess-modal"' in resp.text or 'action="/assess"' in resp.text


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
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found on line {i}: {line.strip()}"


def test_no_inline_styles_fleet(client, _override_store):
    store = _override_store
    store.save(_make_report_scored("fleet-app", 50))
    resp = client.get("/fleet")
    html = resp.text
    lines = html.split("\n")
    for i, line in enumerate(lines, 1):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found on line {i}: {line.strip()}"


def test_no_inline_styles_assess_form(client):
    resp = client.get("/assess")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_assessment_detail(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    resp = client.get(f"/assessments/{aid}")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_events(client):
    resp = client.get("/events")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_gates(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = store.save(report)
    store.create_gate(aid, "deploy", "Test gate")
    resp = client.get("/gates")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


def test_no_inline_styles_onboard_results(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = store.save(report)
    client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = client.get(f"/assessments/{aid}/onboard-results")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
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
    assert "btn-danger-outline" in resp.text
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
    """With no registered agents, watcher agents are still shown."""
    resp = client.get("/agents")
    assert resp.status_code == 200
    assert "Agent Registry" in resp.text
    assert "vuln-watcher" in resp.text
    assert "slo-tracker" in resp.text
    assert "drift-detector" in resp.text


def test_agents_page_with_data(client, _override_store):
    store = _override_store
    store.register_agent("security", "hardening", "network,rbac")
    store.register_agent("observability", "monitoring")
    resp = client.get("/agents")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "observability" in resp.text
    assert "hardening" in resp.text


def test_agents_and_capabilities_are_tabs_of_each_other(client, _override_store):
    """Agents/Capabilities were split top-level nav items; now share one nav
    entry with a tab strip cross-linking Registry (agents) and Catalog
    (capabilities)."""
    agents_resp = client.get("/agents")
    assert 'href="/capabilities"' in agents_resp.text

    capabilities_resp = client.get("/capabilities")
    assert 'href="/agents"' in capabilities_resp.text


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


def test_agent_detail_shows_run_history(client, _override_store):
    store = _override_store
    store.register_agent("security", "hardening", "network,rbac")
    store.save_agent_run("security", "local", "success", duration_ms=1500, resource_tier="standard")
    store.save_agent_run("security", "local", "error", duration_ms=200, error="boom")
    resp = client.get("/agents/security")
    assert resp.status_code == 200
    assert "Run History" in resp.text
    assert "boom" in resp.text


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
    """/agents is no longer a top-level nav item — it's reachable via the
    Capabilities tab strip. The umbrella "Capabilities" link still appears
    globally, and /agents itself links back to /capabilities."""
    resp = client.get("/")
    assert 'href="/capabilities"' in resp.text

    agents_resp = client.get("/agents")
    assert 'href="/capabilities"' in agents_resp.text


# ── Assessment detail shows remediation/SLO buttons ────────────────────


def test_assessment_detail_shows_remediation_button(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.save_remediation(aid, "security", "Fix it")
    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/remediations" in resp.text
    assert "Remediations (1)" in resp.text


def test_assessment_detail_renders_score_history(client, _override_store):
    """Regression: score_history was fetched and passed to the template but
    never rendered — the Overview tab now shows a history table with deltas."""
    store = _override_store
    first = _make_report("history-repo")
    first.scores[0].score = 40
    store.save(first)
    second = _make_report("history-repo")
    second.scores[0].score = 70
    aid2 = store.save(second)

    resp = client.get(f"/assessments/{aid2}")
    assert resp.status_code == 200
    assert "Score History" in resp.text
    assert "score-history-table" in resp.text
    # No new inline styles introduced by the score-history feature.
    history_section = resp.text.split('<table class="score-history-table">')[1].split("</table>")[0]
    assert "style=" not in history_section


def test_assessment_detail_shows_links_with_zero_counts(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "Remediations (0)" in resp.text
    assert "SLOs (0)" in resp.text
    assert "History (0)" in resp.text


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


def test_settings_and_schedules_are_tabs_of_each_other(client, _override_store):
    """Settings/Schedules were split top-level nav items; now share one nav
    entry with a tab strip cross-linking the two pages."""
    settings_resp = client.get("/settings")
    assert '/settings"' in settings_resp.text
    assert 'href="/schedules"' in settings_resp.text

    schedules_resp = client.get("/schedules")
    assert 'href="/settings"' in schedules_resp.text


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
    """/schedules is no longer a top-level nav item — it's reachable via the
    Settings tab strip. The umbrella "Settings" link still appears globally."""
    resp = client.get("/")
    assert 'href="/settings"' in resp.text


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


# ── Health page ────────────────────────────────────────────────────────


def test_health_page(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "System Health" in resp.text


def test_health_api(client):
    resp = client.get("/api/health")
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "pods_running" in data
    assert "pipeline_status" in data


def test_health_nav_link(client):
    resp = client.get("/")
    assert 'href="/health"' in resp.text


def test_pod_detail_404(client):
    """Mock the kube client so this test is hermetic (no live-cluster round trip)."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.core_v1.return_value.read_namespaced_pod.side_effect = Exception("not found")
        resp = client.get("/health/pods/nonexistent-pod-xyz")
    assert resp.status_code == 404


def test_pipeline_detail_404(client):
    """Mock the kube client so this test is hermetic (no live-cluster round trip)."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.get_custom_resource.return_value = None
        resp = client.get("/health/pipelines/nonexistent-run-xyz")
    assert resp.status_code == 404


def test_pod_detail_success(client):
    """Pod detail is served from the kube API client — no `oc` subprocess involved."""
    mock_pod = MagicMock()
    mock_pod.status.phase = "Running"
    mock_pod.metadata.creation_timestamp = None
    cs = MagicMock(name="app", image="quay.io/example/app:latest", ready=True, restart_count=2)
    cs.name = "app"
    mock_pod.status.container_statuses = [cs]

    mock_event = MagicMock()
    mock_event.last_timestamp = None
    mock_event.type = "Warning"
    mock_event.reason = "BackOff"
    mock_event.message = "container restarting"

    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.core_v1.return_value.read_namespaced_pod.return_value = mock_pod
        mock_kube.core_v1.return_value.read_namespaced_pod_log.return_value = "log line 1\n"
        mock_kube.core_v1.return_value.list_namespaced_event.return_value.items = [mock_event]

        resp = client.get("/health/pods/my-pod")

    assert resp.status_code == 200
    assert "Running" in resp.text
    assert "log line 1" in resp.text
    assert "BackOff" in resp.text
    mock_kube.core_v1.return_value.read_namespaced_pod.assert_called_with(
        "my-pod", "agentit", _request_timeout=10,
    )


def test_pipeline_detail_success(client):
    """Pipeline detail is served from the kube custom-objects API — no `oc` subprocess."""
    pipelinerun = {
        "status": {
            "conditions": [{"reason": "Succeeded"}],
            "startTime": "2026-01-01T00:00:00Z",
            "completionTime": "2026-01-01T00:05:00Z",
            "childReferences": [
                {"pipelineTaskName": "git-clone", "conditions": [{"reason": "Succeeded"}], "name": "run-git-clone-pod"},
            ],
        },
    }

    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.get_custom_resource.return_value = pipelinerun
        mock_kube.core_v1.return_value.read_namespaced_pod.return_value.spec.containers = []

        resp = client.get("/health/pipelines/build-my-app-123")

    assert resp.status_code == 200
    assert "Succeeded" in resp.text
    assert "git-clone" in resp.text
    mock_kube.get_custom_resource.assert_called_with(
        "tekton.dev", "v1", "pipelineruns", "build-my-app-123", namespace="agentit",
    )


def test_operator_status_installed(client):
    """Regression test: CSV names are always "<package>.v<version>" (e.g.
    "vertical-pod-autoscaler.v4.21.0-202606301919", verified live against a
    real cluster) -- spec.displayName is a human-readable string ("My
    Operator") that never equals the OLM package name, so matching on it
    (the old behavior) always missed and this endpoint reported "installing"
    forever even after the CSV had actually reached Succeeded."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.return_value = [
            {
                "metadata": {"name": "my-operator.v1.2.3"},
                "spec": {"displayName": "My Operator"},
                "status": {"phase": "Succeeded"},
            },
        ]
        resp = client.get("/api/operator-status?package=my-operator")

    assert resp.status_code == 200
    assert "Installed" in resp.text
    mock_kube.list_custom_resources.assert_called_with(
        "operators.coreos.com", "v1alpha1", "clusterserviceversions", "openshift-my-operator",
    )


def test_operator_status_still_installing(client):
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.return_value = []
        mock_kube.get_custom_resource.return_value = {"status": {"state": "AtLatestKnown"}}
        resp = client.get("/api/operator-status?package=my-operator")

    assert resp.status_code == 200
    assert "AtLatestKnown" in resp.text


def test_operator_status_escapes_reflected_package_param(client):
    """Regression test for docs/code-review-2026-07-12.md item #1: `package`
    is a client-supplied query param interpolated into a raw HTMLResponse
    (bypassing Jinja2 autoescaping), so an unescaped value is reflected XSS."""
    payload = '<script>alert(1)</script>'
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = Exception("cluster unreachable")
        resp = client.get(f"/api/operator-status?package={payload}")

    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


# ── Gate deduplication and expiry ──────────────────────────────────────


def test_gate_deduplication(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    g1 = store.create_gate(aid, "deploy", "First gate")
    g2 = store.create_gate(aid, "deploy", "Duplicate gate")
    assert g1 == g2  # same gate returned


def test_gate_different_types_not_deduped(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    g1 = store.create_gate(aid, "deploy", "Deploy gate")
    g2 = store.create_gate(aid, "security-review", "Security gate")
    assert g1 != g2


def test_stale_gate_expiry(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    store.create_gate(aid, "deploy", "Old gate")
    # Manually backdate the gate
    store._conn.execute(
        "UPDATE gates SET created_at = '2020-01-01T00:00:00' WHERE status = 'pending'"
    )
    store._conn.commit()
    expired = store.expire_stale_gates(hours=1)
    assert expired == 1
    assert len(store.list_gates("pending")) == 0


# ── Delete ─────────────────────────────────────────────────────────────


def test_delete_assessment_route(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    resp = client.post(f"/assessments/{aid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert store.get(aid) is None


def test_delete_cascades(client, _override_store):
    """Delete removes all related data — remediations, SLOs, gates, onboarding."""
    store = _override_store
    aid = store.save(_make_report())
    store.save_remediation(aid, "security", "Fix RBAC")
    store.save_slo(aid, "availability", 99.9)
    store.create_gate(aid, "deploy", "Approve deploy")
    store.save_onboarding(aid, [{"category": "sec", "path": "x.yaml", "content": "y", "description": "d"}])

    resp = client.post(f"/assessments/{aid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert store.get(aid) is None
    assert store.list_remediations(aid) == []
    assert store.list_slos(aid) == []
    assert store.get_onboarding(aid) is None


def test_delete_nonexistent_returns_404(client, _override_store):
    resp = client.post("/assessments/fake-id/delete", follow_redirects=False)
    assert resp.status_code == 404


def test_delete_slo_route(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    sid = store.save_slo(aid, "latency", 200.0)
    resp = client.post(f"/assessments/{aid}/slos/{sid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert len(store.list_slos(aid)) == 0


def test_delete_remediation_route(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    rid = store.save_remediation(aid, "cicd", "Add pipeline")
    resp = client.post(f"/assessments/{aid}/remediations/{rid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert len(store.list_remediations(aid)) == 0


def test_cancel_gate_route(client, _override_store):
    store = _override_store
    aid = store.save(_make_report())
    gid = store.create_gate(aid, "deploy", "Approve")
    resp = client.post(f"/gates/{gid}/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert len(store.list_gates("pending")) == 0


# ── Capabilities: learn (research CVEs & generate skills) ──────────────


def test_capabilities_page_has_learn_button(client):
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    assert '/capabilities/learn' in resp.text
    assert "Research CVEs" in resp.text


def test_capabilities_learn_without_llm_shows_error(client):
    with patch("agentit.portal.app._get_llm_client", return_value=None):
        resp = client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "LLM" in resp.headers["location"]


def test_capabilities_learn_generates_new_skill(client, _override_store):
    with patch("agentit.portal.app._get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00001"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=False), \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: cve-2099-00001\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/cve-2099-00001.md")):
        resp = client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "success=" in resp.headers["location"]
    events = _override_store.list_events_by_agent("learning-agent", limit=5)
    assert any(e["action"] == "skills-generated" for e in events)


def test_capabilities_learn_skips_existing_skill(client, _override_store):
    with patch("agentit.portal.app._get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00002"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=True):
        resp = client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "success=" in resp.headers["location"]
    events = _override_store.list_events_by_agent("learning-agent", limit=5)
    assert not any(e["action"] == "skills-generated" for e in events)


def test_capabilities_learn_no_research_results(client):
    with patch("agentit.portal.app._get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[]):
        resp = client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "success=" in resp.headers["location"]


# ── Capabilities: activate a draft skill ────────────────────────────────


def _make_draft_skill(tmp_path, name="cve-2099-00003", domain="security") -> Path:
    skills_dir = tmp_path / "skills" / domain
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / f"{name}.md"
    skill_file.write_text(
        f"---\n"
        f"name: {name}\n"
        f"domain: {domain}\n"
        f"version: 1\n"
        f"triggers: [test]\n"
        f"outputs: [NetworkPolicy]\n"
        f"status: draft\n"
        f"---\n"
        f"body\n",
        encoding="utf-8",
    )
    return skill_file


def test_activate_skill_promotes_draft_to_active(client, tmp_path, monkeypatch):
    skill_file = _make_draft_skill(tmp_path)
    monkeypatch.chdir(tmp_path)

    resp = client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(skill_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "success=" in resp.headers["location"]
    assert "status: active" in skill_file.read_text()


def test_activate_skill_already_active_shows_error(client, tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills" / "security"
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / "already-active.md"
    skill_file.write_text(
        "---\nname: already-active\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\nstatus: active\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resp = client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(skill_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_activate_skill_rejects_path_outside_skills_dir(client, tmp_path, monkeypatch):
    outside_file = tmp_path / "not-a-skill.md"
    outside_file.write_text("status: draft", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    monkeypatch.chdir(tmp_path)

    resp = client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(outside_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert outside_file.read_text() == "status: draft"


def test_activate_skill_missing_file_shows_error(client, tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    monkeypatch.chdir(tmp_path)

    resp = client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(tmp_path / "skills" / "nope.md")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ── Capabilities: catalog change tracking (skill_inventory) ────────────


def test_capabilities_page_renders_catalog_changes_section(client):
    """The 'Recent Catalog Changes' section should always render, even empty."""
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    assert "Recent Catalog Changes" in resp.text


def test_capabilities_page_shows_catalog_change_events(client, _override_store):
    store = _override_store
    store.log_event("skill-inventory", "skill-added", None, "info",
                     "New skill added: security/cve-2099-1")
    store.log_event("skill-inventory", "check-removed", None, "warning",
                     "Check removed: reliability/has-readiness-probe")

    resp = client.get("/capabilities")
    assert resp.status_code == 200
    assert "New skill added: security/cve-2099-1" in resp.text
    assert "Check removed: reliability/has-readiness-probe" in resp.text
    assert "badge-success" in resp.text
    assert "badge-warning" in resp.text


def test_background_skill_inventory_diff_surfaces_on_events_page(client, _override_store, tmp_path, monkeypatch):
    """Simulates a tick of `_background_maintenance()`'s inventory-diff step
    without waiting for the real hourly loop, then confirms the resulting
    events show up on /events (and thus on the Capabilities page too)."""
    from agentit.skill_inventory import diff_and_log_inventory_changes

    skills_dir = tmp_path / "skills"
    checks_dir = tmp_path / "checks"
    security_dir = skills_dir / "security"
    security_dir.mkdir(parents=True)
    (security_dir / "netpol-basic.md").write_text(
        "---\nname: netpol-basic\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\n---\nbody\n",
        encoding="utf-8",
    )

    store = _override_store
    # First tick: no prior snapshot -> seeds baseline, no events yet.
    diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)
    assert store.list_events_by_agent("skill-inventory") == []

    # A new skill lands on disk (as if a PR merged to skills/).
    (security_dir / "cve-2099-1.md").write_text(
        "---\nname: cve-2099-1\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\n---\nbody\n",
        encoding="utf-8",
    )

    # Second tick: diff finds the addition and logs an event.
    diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

    events = store.list_events_by_agent("skill-inventory")
    assert len(events) == 1
    assert events[0]["action"] == "skill-added"

    resp = client.get("/events")
    assert resp.status_code == 200
    assert "skill-added" in resp.text
    assert "cve-2099-1" in resp.text

    caps_resp = client.get("/capabilities")
    assert caps_resp.status_code == 200
    assert "cve-2099-1" in caps_resp.text


# ── LLM Decisions audit page ─────────────────────────────────────────────


def test_decisions_page_renders_empty_state(client):
    resp = client.get("/decisions")
    assert resp.status_code == 200
    assert "LLM Decisions" in resp.text
    assert "No LLM decisions logged yet" in resp.text


def test_decisions_page_shows_fix_review_and_auto_mode_decisions(client, _override_store):
    store = _override_store
    store.record_skill_outcome("network-policy", "my-app", "approved", "Fix is correct and safe")
    store.log_event("HardeningAgent", "decision", "other-app", "info",
                     "AUTO-APPLY: LLM classified as safe (0.95): Adds a ConfigMap")

    resp = client.get("/decisions")
    assert resp.status_code == 200
    assert "network-policy" in resp.text
    assert "Fix is correct and safe" in resp.text
    assert "HardeningAgent" in resp.text
    assert "Adds a ConfigMap" in resp.text
    assert "auto-applied" in resp.text


def test_decisions_page_filters_by_attribution(client, _override_store):
    store = _override_store
    store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
    store.record_skill_outcome("containerfile", "app-a", "rejected", "wrong base image")

    resp = client.get("/decisions?attribution=containerfile")
    assert resp.status_code == 200
    assert "wrong base image" in resp.text
    # "fine" (network-policy's reason) shouldn't appear in the filtered decision
    # log or summary — network-policy itself may still appear in the filter
    # dropdown's <option> list, which is built from the unfiltered attribution set.
    assert ">fine<" not in resp.text


def test_decisions_page_filters_by_decision_type(client, _override_store):
    store = _override_store
    store.record_skill_outcome("network-policy", "app-a", "approved", "fine skill decision")
    store.log_event("auto-mode", "decision", "app-b", "info", "AUTO-APPLY: safe: fine auto decision")

    resp = client.get("/decisions?decision_type=fix-review")
    assert resp.status_code == 200
    assert "fine skill decision" in resp.text
    assert "fine auto decision" not in resp.text
