from __future__ import annotations

import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

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
from agentit.platform_context import PlatformContext
from agentit.portal.app import app, get_store
from agentit.portal.routes.assessments import _get_trusted_base_url

# Empty PlatformContext is FleetOrchestrator.run()'s "discovery never
# actually connected" signal, which skips the has_api() gate entirely
# (platform=None) -- pinning to this keeps onboarding tests' skill
# output independent of whatever cluster happens to be reachable when
# the suite runs. See FleetOrchestrator.run() for the exact fallback.
_NO_CLUSTER = PlatformContext()
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
async def _override_store():
    """Patch get_store so every test gets the real, truncated-fresh store.

    Also patches image_builder.build_app_image: onboarding a report whose
    findings include a missing Containerfile causes HardeningAgent to
    generate one, which flips `has_containerfile` in app.py and triggers a
    REAL `oc apply` PipelineRun via subprocess — against whatever cluster
    the local kubeconfig happens to point to. Without this patch, running
    this suite with an active `oc login` silently floods a real cluster.
    """
    test_store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=test_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=test_store), \
         patch("agentit.portal.routes.health.get_store", return_value=test_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=test_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=test_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=test_store), \
         patch("agentit.portal.routes.gates.get_store", return_value=test_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=test_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=test_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=test_store), \
         patch("agentit.portal.routes.remediations.get_store", return_value=test_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=test_store), \
         patch("agentit.image_builder.build_app_image",
               return_value={"image_ref": "test/image:test", "run_name": "test-run", "status": "skipped-in-tests"}):
        yield test_store


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


async def test_dashboard_empty(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Assess your first app" in resp.text


async def test_assess_form(client):
    resp = await client.get("/assess")
    assert resp.status_code == 200
    assert "<form" in resp.text
    assert "repo_url" in resp.text


async def test_api_list_empty(client):
    resp = await client.get("/api/assessments")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_save_and_retrieve(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)

    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "test-repo" in resp.text


async def test_api_roundtrip(client, _override_store):
    store = _override_store
    report = _make_report("my-service")
    aid = await store.save(report)

    resp = await client.get(f"/api/assessments/{aid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo_name"] == "my-service"
    assert "scores" in data


async def test_assessment_not_found(client):
    resp = await client.get("/assessments/nonexistent")
    assert resp.status_code == 404


async def test_api_detail_not_found(client):
    resp = await client.get("/api/assessments/nonexistent")
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


async def test_onboard_creates_results(client, _override_store):
    """Onboard runs as a background job (docs/ux-design-requirements.md
    checklist #6/#8) -- the POST redirects to a real-time progress page,
    not straight to onboard-results. TestClient awaits background tasks to
    completion before returning the response, so the job is already
    "completed" by the time the progress page's own redirect is followed."""
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    resp = await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    assert resp.status_code == 303
    assert f"/assessments/{aid}/onboard/progress/" in resp.headers["location"]

    progress_resp = await client.get(resp.headers["location"], follow_redirects=False)
    assert progress_resp.status_code == 303
    assert f"/assessments/{aid}/onboard-results" in progress_resp.headers["location"]


async def test_onboard_results_page(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    # Pin platform discovery so skill generation doesn't depend on
    # whatever cluster happens to be reachable at test time (see
    # FleetOrchestrator.run()'s platform=None fallback).
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = await client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 200
    # security/observability/compliance are now skill-only domains (see
    # docs/agent-removal-readiness.md) -- generated manifests are grouped
    # under the shared "skills" category instead of one per domain.
    assert "skills" in resp.text
    assert "Download" in resp.text
    # The unified apply flow (docs/unified-apply-flow.md) collapsed the
    # independent "Apply to Cluster" / "Create PR" buttons into one
    # dynamically-labeled Deliver action -- GitOps → "Commit & Open PR",
    # Direct → "Apply to Cluster". This report has no infra_repo_url and
    # isn't GitOps-registered, so the button reads "Apply to Cluster".
    assert "Apply to Cluster" in resp.text
    assert "Commit & Open PR" not in resp.text
    assert "Deliver Now" not in resp.text
    assert "Per-Agent PRs" in resp.text
    assert "Dry Run" in resp.text
    assert "delivery-actions" in resp.text
    assert "delivery-primary" in resp.text
    assert "delivery-secondary" in resp.text
    assert "delivery-connector" in resp.text
    # Status chip lives outside the Apply CTA (not packed into the button).
    assert "No dry run yet" in resp.text
    assert "NO DRY RUN YET" not in resp.text
    # Soft-gate: primary Apply disabled until Dry Run succeeds; override remains.
    assert 'data-action="apply"' in resp.text
    assert "disabled" in resp.text
    assert "Override — Apply to Cluster anyway" in resp.text


async def test_api_manifests(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    # Pin platform discovery — see comment in test_onboard_results_page.
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = await client.get(f"/api/assessments/{aid}/manifests")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    # security/observability/compliance are now skill-only domains (see
    # docs/agent-removal-readiness.md) -- grouped under "skills" instead.
    categories = {f["category"] for f in data}
    assert "skills" in categories


async def test_api_manifests_not_found(client):
    resp = await client.get("/api/assessments/nonexistent/manifests")
    assert resp.status_code == 404


async def test_onboard_not_found(client):
    resp = await client.post("/assessments/nonexistent/onboard", follow_redirects=False)
    assert resp.status_code == 404


async def test_trusted_base_url_ignores_forged_host_header(client, _override_store):
    """A forged Host header must not affect the webhook URL registered with GitHub.

    Regression test for the reflected-Host-header issue: with no
    AGENTIT_EXTERNAL_URL override and no reachable cluster Route, the request's
    Host header used to be trusted outright.
    """
    store = _override_store
    report = _make_report()
    aid = await store.save(report)

    fake_routes = [
        {"metadata": {"labels": {"app.kubernetes.io/name": "agentit"}},
         "spec": {"host": "agentit.apps.cluster.example.com"}},
    ]
    with patch("agentit.portal.routes.assessments._run_onboarding", return_value=([], {})), \
         patch("agentit.portal.github_pr.ensure_webhook") as mock_ensure_webhook, \
         patch("agentit.kube.list_custom_resources", return_value=fake_routes), \
         patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
        mock_ensure_webhook.return_value = {"created": True}
        await client.post(
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


async def test_onboard_results_no_onboarding(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)

    resp = await client.get(f"/assessments/{aid}/onboard-results")
    assert resp.status_code == 404


async def test_download_manifests_zip(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    # Run onboarding to populate files. Pin platform discovery so skill
    # generation doesn't depend on whatever cluster happens to be
    # reachable at test time (see FleetOrchestrator.run()'s platform=None
    # fallback).
    with patch("agentit.platform_context.discover_platform", return_value=_NO_CLUSTER):
        await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)

    resp = await client.get(f"/api/assessments/{aid}/manifests/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "onboard-repo-onboarding.zip" in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) > 0

    # Every entry should be under a known category directory.
    # security/observability/compliance are now skill-only domains (see
    # docs/agent-removal-readiness.md) -- grouped under "skills" instead.
    categories = {n.split("/")[0] for n in names}
    assert "skills" in categories

    # Verify files are readable and non-empty
    for name in names:
        assert len(zf.read(name)) > 0
    zf.close()


async def test_download_manifests_not_found(client):
    resp = await client.get("/api/assessments/nonexistent/manifests/download")
    assert resp.status_code == 404


async def test_download_manifests_no_onboarding(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)

    resp = await client.get(f"/api/assessments/{aid}/manifests/download")
    assert resp.status_code == 404


async def test_verify_properties_not_found(client):
    resp = await client.get("/api/assessments/nonexistent/verify")
    assert resp.status_code == 404


async def test_verify_properties_no_onboarding(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)

    resp = await client.get(f"/api/assessments/{aid}/verify")
    assert resp.status_code == 404


async def test_verify_properties_runs_against_generated_files(client, _override_store):
    """Regression: this endpoint used to call verify_all_properties(repo_name,
    repo_name) -- two strings -- while verify_all_properties() expects a
    list[GeneratedFile]. Any real call would raise a TypeError."""
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    await store.save_onboarding(aid, [
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

    resp = await client.get(f"/api/assessments/{aid}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == report.repo_name
    assert "results" in body
    properties = {r["property"] for r in body["results"]}
    assert "Network Isolation" in properties


async def test_assessment_detail_has_onboard_button(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)

    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/onboard" in resp.text
    assert "Onboard This App" in resp.text
    assert "btn-action btn-lg" in resp.text
    assert "&larr; Fleet" in resp.text or "← Fleet" in resp.text or "Fleet</a>" in resp.text
    assert "Dashboard" not in resp.text or "Fleet" in resp.text


async def test_assessment_detail_lifecycle_primary_cta_after_onboard(client, _override_store):
    """Once onboarded, Review & Deliver is primary — not a giant Onboard."""
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    await store.save_onboarding(aid, [{
        "category": "skills",
        "path": "netpol.yaml",
        "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
        "description": "test",
    }])

    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "Review &amp; Deliver" in resp.text or "Review & Deliver" in resp.text
    assert f'href="/assessments/{aid}/onboard-results"' in resp.text
    assert "Re-onboard" in resp.text
    assert "Onboard This App" not in resp.text


async def test_fleet_h1_not_enterprise_readiness(client, _override_store):
    store = _override_store
    await store.save(_make_report())
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "<h1>Fleet</h1>" in resp.text
    assert "Enterprise Readiness" not in resp.text


# ------------------------------------------------------------------
# Events & Webhook tests
# ------------------------------------------------------------------


async def test_masthead_nav_structure(client, _override_store):
    """Ledger is primary nav; Decisions is in the user/main menu; Events is
    a bell icon + drawer (full page still at /events). No Activity dropdown."""
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "events-bell" in html
    assert "events-bell-badge" in html
    assert 'id="events-drawer"' in html
    assert "eventsDrawer()" in html
    assert "/api/events?limit=20" in html
    # Badge polls the same real feed (slightly larger window for unread).
    assert "/api/events?limit=50" in html
    assert "agentit.events.lastSeenAt" in html
    assert "refreshBadge" in html
    assert "_isBadgeSeverity" in html
    assert "_eventHref" in html
    assert "activity-menu" not in html
    # Bell shares Alpine scope with the drawer (aria-expanded + focus restore).
    assert ':aria-expanded="open"' in html
    assert 'x-ref="bellBtn"' in html
    assert 'x-ref="closeBtn"' in html
    assert 'x-ref="drawerPanel"' in html
    # Mobile hamburger owns primary + secondary (not secondary-only).
    assert 'id="nav-primary"' in html
    assert 'id="nav-secondary"' in html
    assert 'aria-controls="nav-primary nav-secondary"' in html
    assert ":aria-expanded=\"navOpen\"" in html
    assert 'nav .links.links-open' in html
    # Cmd+K search is visually centered and a bit wider than content-sized.
    assert "cmdk-trigger" in html
    assert "cmdk-trigger-label" in html
    assert "left: 50%" in html
    assert "translateX(-50%)" in html
    assert "min-width: 14rem" in html
    # Single "View all" CTA (footer), not duplicated in the header.
    drawer = html.split('id="events-drawer-panel"', 1)[1].split("id=\"nav-loading\"", 1)[0]
    assert drawer.count(">View all<") == 1
    assert "Open full Events page" not in drawer
    assert "Run an assessment from Fleet" in drawer
    assert "events-drawer-empty-hint" in drawer
    primary = html.split('class="links"', 1)[1].split("links-secondary", 1)[0]
    assert 'href="/ledger"' in primary
    assert 'href="/decisions"' not in primary
    assert 'href="/events"' not in primary
    dropdown_idx = html.index("user-menu-dropdown")
    decisions_in_menu = html.index('href="/decisions"', dropdown_idx)
    assert decisions_in_menu > dropdown_idx


async def test_events_page_renders(client, _override_store):
    store = _override_store
    await store.log_event("test-agent", "scan", "my-app", "high", "Found vuln")
    resp = await client.get("/events")
    assert resp.status_code == 200
    assert "Agent Activity Feed" in resp.text
    assert "test-agent" in resp.text
    assert "Found vuln" in resp.text


async def test_api_events_returns_real_events(client, _override_store):
    """Events drawer fetches this endpoint — must return real store rows."""
    store = _override_store
    await store.log_event("drawer-agent", "scan", "drawer-app", "info", "drawer visible")
    resp = await client.get("/api/events?limit=20")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(e.get("summary") == "drawer visible" for e in data)


async def test_events_page_filters_by_correlation_id(client, _override_store):
    store = _override_store
    await store.log_event("orchestrator", "completed", "app-a", "info", "chain event", correlation_id="chain-xyz")
    await store.log_event("orchestrator", "completed", "app-b", "info", "other event", correlation_id="chain-other")
    resp = await client.get("/events?correlation_id=chain-xyz")
    assert resp.status_code == 200
    assert "chain event" in resp.text
    assert "other event" not in resp.text


async def test_events_target_app_links_to_assessment_when_resolvable(client, _override_store):
    """Every other page (Fleet, Remediations, Decisions) links an app name
    to its Assessment Detail page -- Events showed plain text instead."""
    store = _override_store
    aid = await store.save(_make_report("linked-app"))
    await store.log_event("test-agent", "scan", "linked-app", "info", "did a thing")

    resp = await client.get("/events")
    assert resp.status_code == 200
    assert f'<a href="/assessments/{aid}">linked-app</a>' in resp.text


# ------------------------------------------------------------------
# Ledger tests (docs/ledger-design-spec.md Phase 1)
# ------------------------------------------------------------------


async def test_ledger_page_renders_fleet_wide_cards(client, _override_store):
    """Default view is grouped by app (docs/ledger-design-spec.md §2 rule 2)
    -- the app's row shows its own most-recent card summarized inline and
    links back to that app's Assessment Detail Ledger tab. A freshly
    assessed app with no open gates isn't "Needs You" (rule 3), so this
    disables that filter to check the underlying grouped rendering itself."""
    store = _override_store
    aid = await store.save(_make_report("ledger-fleet-app"))

    resp = await client.get("/ledger?needs_you=0")
    assert resp.status_code == 200
    assert "Ledger" in resp.text
    assert "assessment-complete" in resp.text
    assert f'<a href="/assessments/{aid}?tab=ledger">ledger-fleet-app</a>' in resp.text


async def test_ledger_page_filters_by_card_type(client, _override_store):
    """Card-type filtering is exercised in the flat stream (?view=flat) --
    a watcher tick has no target_app, so it can never appear in a
    grouped-by-app row (docs/ledger-design-spec.md §2 rule 2 is strictly
    per-app; untargeted fleet-level events live in the flat view only)."""
    store = _override_store
    await store.save(_make_report("card-a-app"))
    await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")

    resp = await client.get("/ledger?view=flat&card_type=H")
    assert resp.status_code == 200
    assert "tick-complete" in resp.text
    assert "assessment-complete" not in resp.text


async def test_ledger_page_filters_by_app(client, _override_store):
    store = _override_store
    await store.save(_make_report("app-one"))
    await store.save(_make_report("app-two"))

    resp = await client.get("/ledger?app=app-one")
    assert resp.status_code == 200
    assert "app-one" in resp.text
    assert "app-two" not in resp.text


async def test_ledger_page_empty_state(client, _override_store):
    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "No Ledger activity yet" in resp.text


async def test_ledger_nav_link_present(client, _override_store):
    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert 'href="/ledger"' in resp.text


# ------------------------------------------------------------------
# Next-step hint (visual hierarchy pass): ties the lifecycle stepper to
# the actual action that moves the app forward, pending actions win
# regardless of stage.
# ------------------------------------------------------------------


async def test_next_step_hint_prompts_onboarding_when_freshly_assessed(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report("fresh-app"))
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "Ready to onboard" in resp.text
    assert "next-step-hint" in resp.text
    hint = resp.text.split('class="next-step-hint"', 1)[1].split("</div>", 1)[0]
    assert "pending action" not in hint


async def test_next_step_hint_prioritizes_pending_actions_over_stage(client, _override_store):
    """A pending gate is the most urgent thing regardless of lifecycle
    stage -- must win even for a freshly-assessed app that hasn't been
    onboarded yet (e.g. a gate created via the per-finding Fix flow)."""
    store = _override_store
    aid = await store.save(_make_report("gated-app"))
    await store.create_gate(aid, "auto-mode-review", "needs review")
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "1</strong> pending action" in resp.text
    assert "Ready to onboard" not in resp.text
    # Regression: the hint sits outside the tab strip's x-data, so an
    # Alpine @click="tab = 'actions'" is a dead handler. Must be a real
    # ?tab=actions href (same convention as Fleet's pending badge).
    hint = resp.text.split('class="next-step-hint"', 1)[1].split("</div>", 1)[0]
    assert f'href="/assessments/{aid}?tab=actions"' in hint
    assert "@click.prevent=\"tab = 'actions'\"" not in hint
    assert "@click=\"tab = 'actions'\"" not in hint


# ------------------------------------------------------------------
# Finding source badge display (masthead/UI cleanup pass)
# ------------------------------------------------------------------


async def test_finding_source_badge_strips_absolute_path(client, _override_store):
    """A data-driven check's real source is an absolute filesystem path
    (check_engine.py's source_path=str(path)) -- deployment-location-
    dependent and not meaningful to a human reader. The rendered badge
    must show only the checks/... portion; the hidden check_source form
    field /api/suppress matches against must stay the exact raw value, so
    existing suppression records keep matching unchanged."""
    store = _override_store
    report = _make_report("path-cleanup-app")
    report.scores[0].findings.append(
        Finding(
            category="ci-pipeline",
            severity=Severity.high,
            description="No GitLab CI pipeline configuration found",
            recommendation="Add a .gitlab-ci.yml",
            source="check:/opt/app-root/src/checks/cicd/ci-pipeline.yaml",
        )
    )
    aid = await store.save(report)

    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert '<span class="badge badge-muted finding-source">check:checks/cicd/ci-pipeline.yaml</span>' in resp.text
    # The hidden suppress form field must keep the exact raw source, unchanged
    # -- only the visible badge is cleaned up, not the value suppressions match on.
    assert 'name="check_source" value="check:/opt/app-root/src/checks/cicd/ci-pipeline.yaml"' in resp.text


async def test_ledger_needs_you_filter_hides_healthy_apps_by_default(client, _override_store):
    """docs/ledger-design-spec.md §2 rule 3: "Needs You" (on by default)
    shows only apps with a pending gate, a stale gate, or an unresolved SLO
    breach -- a healthy app with no open work is hidden until toggled off."""
    store = _override_store
    await store.save(_make_report("healthy-app"))
    needs_aid = await store.save(_make_report("needs-app"))
    await store.create_gate(needs_aid, "finding-security", "Review this finding")

    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "needs-app" in resp.text
    assert "healthy-app" not in resp.text

    resp_all = await client.get("/ledger?needs_you=0")
    assert resp_all.status_code == 200
    assert "needs-app" in resp_all.text
    assert "healthy-app" in resp_all.text


async def test_ledger_watcher_failure_banner_shown_within_window(client, _override_store):
    """The 4th "Needs You" signal (a watcher's last tick failed recently) is
    fleet-wide, not attributable to one app row, so it renders as its own
    banner instead of a per-app filter criterion."""
    store = _override_store
    await store.log_event("vuln-watcher", "tick-failed", None, "error", "vuln-watcher tick failed: boom")

    resp = await client.get("/ledger")
    assert resp.status_code == 200
    assert "watcher tick failure" in resp.text.lower()
    assert "vuln-watcher" in resp.text


async def test_ledger_chain_route_replays_a_real_chain_read_only(client, _override_store):
    """docs/ledger-design-spec.md §4: the rewind scrubber. store.save()
    already logs its own "assessment-complete" event with
    correlation_id=assessment_id, so every assessed app has a real,
    one-card-so-far chain to replay -- no fabricated correlation id needed."""
    store = _override_store
    aid = await store.save(_make_report("chain-route-app"))
    await store.create_gate(aid, "finding-security", "Review this finding")

    resp = await client.get(f"/ledger/chain/{aid}")
    assert resp.status_code == 200
    assert "Replay this chain" in resp.text
    assert "assessment-complete" in resp.text
    assert "finding-security" in resp.text
    assert 'action="/gates/' not in resp.text  # read-only: no gate-resolution form here


async def test_ledger_chain_route_empty_for_unknown_correlation_id(client, _override_store):
    resp = await client.get("/ledger/chain/does-not-exist")
    assert resp.status_code == 200
    assert "No chain found" in resp.text


async def test_events_target_app_plain_text_when_no_assessment_resolves(client, _override_store):
    """Never fabricate a link target -- an event whose target_app doesn't
    match any known app must render as plain text, not a broken link."""
    store = _override_store
    await store.log_event("test-agent", "scan", "unknown-app", "info", "did a thing")

    resp = await client.get("/events")
    assert resp.status_code == 200
    assert "unknown-app" in resp.text
    # No assessment exists at all in this test -- there must be no
    # assessment link anywhere on the page, let alone a fabricated one.
    assert 'href="/assessments/' not in resp.text


async def test_dlq_retry_republishes_and_redirects(client, _override_store):
    store = _override_store
    eid = await store.log_event(
        "event-consumer", "dead-letter", "app", "error", "Dead-lettered",
        details={
            "original_topic": "agentit-events",
            "original_message": {"agentId": "x", "action": "tick", "result": {"summary": "", "details": {}}},
            "error": "boom",
        },
    )
    resp = await client.post(f"/events/dlq/{eid}/retry", follow_redirects=False)
    assert resp.status_code == 303
    assert "success" in resp.headers["location"]
    assert await store.list_dlq_messages() == []


async def test_insights_page_shows_fleet_wide_feedback(client, _override_store):
    """Regression: insights_page used get_feedback_for_app(""), which filters
    on app_name = '' and always returns nothing -- get_all_feedback fixes it."""
    store = _override_store
    await store.record_feedback("app-a", "security", "network-policy", "rejected", human_reason="not needed here")
    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert "not needed here" in resp.text


async def test_insights_page_shows_check_compliance(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_check_results(aid, [
        {"check_name": "has-network-policy", "dimension": "security", "passed": True},
    ])
    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert "Fleet-Wide Check Compliance" in resp.text
    assert "has-network-policy" in resp.text


async def test_insights_agent_performance_zero_success_rate_is_colored_danger(client, _override_store):
    """Regression: the success-rate bar's own color fill is invisible at
    0% (CSS `width: var(--pct)` -> a literal 0px-wide colored bar) --
    confirmed live: a 0%-success agent looked identical to a 100%-success
    one. The percentage label itself must carry the severity color too."""
    store = _override_store
    await store.register_agent("remediation-loop", "remediation")
    for _ in range(3):
        await store.save_agent_run("remediation-loop", "local", "error")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    row = resp.text.split("remediation-loop", 1)[1].split("</tr>", 1)[0]
    assert 'class="text-sm text-danger"' in row
    assert "0.0%" in row or "0%" in row


async def test_insights_agent_performance_full_success_rate_is_colored_success(client, _override_store):
    store = _override_store
    await store.register_agent("hardening", "security")
    await store.save_agent_run("hardening", "local", "success")
    await store.save_agent_run("hardening", "local", "success")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    row = resp.text.split("hardening", 1)[1].split("</tr>", 1)[0]
    assert 'class="text-sm text-success"' in row


async def test_webhook_triggers_assessment(client, _override_store):
    report = _make_report("webhook-repo")
    with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=report):
        resp = await client.post(
            "/api/webhook/assess",
            json={"repo_url": "https://github.com/org/webhook-repo", "criticality": "high"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "assessment_id" in data
    assert "overall_score" in data


async def test_webhook_missing_repo_url(client):
    resp = await client.post("/api/webhook/assess", json={"criticality": "high"})
    assert resp.status_code == 400


async def test_webhook_onboard_triggers_onboarding(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)

    fake_files = [{"category": "security", "path": "netpol.yaml", "content": "kind: NetworkPolicy", "description": "netpol"}]
    fake_summary = {"agents": [], "conflicts": [], "recommendation": "READY", "auto_approve": False, "gates": []}
    with patch("agentit.portal.routes.webhooks.run_onboarding", return_value=(fake_files, fake_summary)):
        resp = await client.post(
            "/api/webhook/onboard",
            json={"correlationId": aid},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["assessment_id"] == aid
    assert data["files_generated"] == 1
    assert "security" in data["categories"]


async def test_webhook_onboard_missing_assessment_id(client):
    resp = await client.post("/api/webhook/onboard", json={"eventId": "evt-123"})
    assert resp.status_code == 400


async def test_webhook_onboard_assessment_not_found(client):
    resp = await client.post(
        "/api/webhook/onboard",
        json={"correlationId": "nonexistent-id"},
    )
    assert resp.status_code == 404


async def test_api_events_returns_json(client, _override_store):
    store = _override_store
    await store.log_event("agent-a", "deploy", "app-x", "info", "Deployed v2")
    await store.log_event("agent-b", "scan", "app-y", "medium", "Scan done")
    resp = await client.get("/api/events")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 2


async def test_api_events_filter_target_app(client, _override_store):
    store = _override_store
    await store.log_event("a", "x", "app-1", "info", "e1")
    await store.log_event("b", "y", "app-2", "info", "e2")
    resp = await client.get("/api/events?target_app=app-1")
    assert resp.status_code == 200
    data = resp.json()
    assert all(e["target_app"] == "app-1" for e in data)


# ------------------------------------------------------------------
# Gate queue tests
#
# The global "/gates" page is retired (docs/ui-redesign-proposal.md §2/§5):
# it now redirects to "/admin-review", which shows only `cluster-admin-review`
# gates -- the one gate type that's genuinely cross-app, for a genuinely
# different audience than an app owner. The other 7 (app-owner-scoped) gate
# types now live on Assessment Detail's Actions tab -- see
# test_admin_review_and_actions_tab.py for that behavior.
# ------------------------------------------------------------------


async def test_gates_redirects_to_admin_review(client):
    resp = await client.get("/gates", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/admin-review"


async def test_admin_review_page_empty(client):
    resp = await client.get("/admin-review")
    assert resp.status_code == 200
    assert "No pending gates" in resp.text


async def test_admin_review_page_with_pending(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    await store.create_gate(aid, "cluster-admin-review", "Approve deployment of test-repo")

    resp = await client.get("/admin-review")
    assert resp.status_code == 200
    assert "Approve deployment of test-repo" in resp.text
    assert "Approve" in resp.text
    assert "Reject" in resp.text


async def test_admin_review_page_excludes_app_owner_gate_types(client, _override_store):
    """A "deploy"-style app-owner gate must never show up on Admin Review --
    that's exactly the audience split docs/ui-redesign-proposal.md §2 makes.
    It belongs on that app's own Assessment Detail Actions tab instead."""
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    await store.create_gate(aid, "deploy", "Approve deployment of test-repo")

    resp = await client.get("/admin-review")
    assert resp.status_code == 200
    assert "Approve deployment of test-repo" not in resp.text
    assert "No pending gates" in resp.text


async def test_resolve_gate_approve(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    gate_id = await store.create_gate(aid, "deploy", "Approve deployment of test-repo")

    resp = await client.post(
        f"/gates/{gate_id}/resolve",
        data={"status": "approved", "resolved_by": "tester"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # A per-app ("deploy") gate with no onboarding files to deliver falls
    # through to the generic resolve path, which lands back on that app's
    # own Assessment Detail Actions tab (not the Overview tab, and not the
    # retired global Gates page) -- the next pending gate in the same
    # queue is immediately visible there (docs/ux-design-requirements.md
    # checklist #12).
    assert resp.headers["location"] == f"/assessments/{aid}?tab=actions"

    pending = await store.list_gates(status="pending")
    assert len(pending) == 0
    approved = await store.list_gates(status="approved")
    assert len(approved) == 1
    assert approved[0]["resolved_by"] == "tester"


async def test_resolve_cluster_admin_review_gate_redirects_to_admin_review(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    gate_id = await store.create_gate(aid, "cluster-admin-review", "CI/CD manifests need elevated review")

    resp = await client.post(
        f"/gates/{gate_id}/resolve",
        data={"status": "rejected", "resolved_by": "tester", "reason": "not now"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin-review"


async def test_list_gates_includes_app_name(client, _override_store):
    """docs/ui-redesign-proposal.md §2's confirmed defect: gates never
    carried which app they belonged to. list_gates()/list_all_gates() now
    join back to the assessment so every gate row is attributable."""
    store = _override_store
    report = _make_report("attributable-app")
    aid = await store.save(report)
    await store.create_gate(aid, "deploy", "Some gate summary with no app name in it")

    pending = await store.list_gates(status="pending")
    assert len(pending) == 1
    assert pending[0]["app_name"] == "attributable-app"

    all_gates = await store.list_all_gates()
    assert len(all_gates) == 1
    assert all_gates[0]["app_name"] == "attributable-app"


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


async def test_fleet_dashboard_shows_portfolio_summary(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("alpha-svc", 80, "low"))
    await store.save(_make_report_scored("beta-svc", 30, "critical"))
    await store.save(_make_report_scored("gamma-svc", 55, "medium"))

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "alpha-svc" in resp.text
    assert "beta-svc" in resp.text
    assert "gamma-svc" in resp.text
    assert "Assess New Repo" in resp.text


async def test_fleet_empty(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Assess your first app" in resp.text


async def test_fleet_redirects_to_home(client):
    resp = await client.get("/fleet", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/"


async def test_api_fleet_returns_json(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("svc-a", 75))
    await store.save(_make_report_scored("svc-b", 40))

    resp = await client.get("/api/fleet")
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


async def test_api_fleet_trend_with_multiple_assessments(client, _override_store):
    store = _override_store
    r1 = _make_report_scored("trending", 40)
    await store.save(r1)
    r2 = _make_report_scored("trending", 60)
    r2.assessed_at = datetime(2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    await store.save(r2)

    resp = await client.get("/api/fleet")
    data = resp.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["latest_score"] == 60
    assert entry["previous_score"] == 40
    assert entry["delta"] == 20.0
    assert entry["assessment_count"] == 2


async def test_dashboard_shows_portfolio_summary(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("app-one", 90))
    await store.save(_make_report_scored("app-two", 60))

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Apps" in resp.text
    assert "Avg Score" in resp.text
    assert "Critical" in resp.text


async def test_fleet_has_assess_modal(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="assess-modal"' in resp.text or 'action="/assess"' in resp.text


# ------------------------------------------------------------------
# UI design system tests
# ------------------------------------------------------------------


async def test_base_has_htmx_script(client):
    resp = await client.get("/")
    assert "htmx.org@2.0.4" in resp.text
    assert 'integrity="sha384-' in resp.text


async def test_base_has_alpinejs_script(client):
    resp = await client.get("/")
    assert "alpinejs@3" in resp.text
    assert 'crossorigin="anonymous"' in resp.text


async def test_base_has_hx_boost(client):
    resp = await client.get("/")
    assert 'hx-boost="true"' in resp.text


async def test_base_has_css_variables(client):
    resp = await client.get("/")
    assert "--color-bg:" in resp.text
    assert "--color-accent:" in resp.text
    assert "--color-surface:" in resp.text
    assert "--radius-md:" in resp.text
    assert "--space-" in resp.text


async def test_no_inline_styles_dashboard(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("styled-app", 70))
    resp = await client.get("/")
    html = resp.text
    lines = html.split("\n")
    for i, line in enumerate(lines, 1):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found on line {i}: {line.strip()}"


async def test_no_inline_styles_fleet(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("fleet-app", 50))
    resp = await client.get("/fleet")
    html = resp.text
    lines = html.split("\n")
    for i, line in enumerate(lines, 1):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found on line {i}: {line.strip()}"


async def test_no_inline_styles_assess_form(client):
    resp = await client.get("/assess")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


async def test_no_inline_styles_assessment_detail(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    resp = await client.get(f"/assessments/{aid}")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


async def test_no_inline_styles_events(client):
    resp = await client.get("/events")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


async def test_no_inline_styles_gates(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    await store.create_gate(aid, "cluster-admin-review", "Test gate")
    resp = await client.get("/admin-review")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


async def test_no_inline_styles_onboard_results(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)
    await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = await client.get(f"/assessments/{aid}/onboard-results")
    html = resp.text
    for line in html.split("\n"):
        if "style=" in line.lower() and 'style="--pct' not in line:
            assert False, f"Inline style found: {line.strip()}"


async def test_dashboard_uses_design_system_classes(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("css-app", 60))
    resp = await client.get("/")
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


async def test_fleet_uses_design_system_classes(client, _override_store):
    store = _override_store
    await store.save(_make_report_scored("fleet-css", 80))
    resp = await client.get("/fleet")
    assert "stat-grid" in resp.text
    assert "row-border-" in resp.text
    assert "text-bold" in resp.text
    assert "btn btn-sm" in resp.text


async def test_assessment_detail_uses_design_system_classes(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    resp = await client.get(f"/assessments/{aid}")
    assert "score-hero" in resp.text
    assert "score-unit" in resp.text
    assert "section-title" in resp.text
    assert "dimension-row" in resp.text
    assert "dimension-label" in resp.text
    assert "dimension-bar" in resp.text
    assert "dimension-value" in resp.text
    assert "finding-list" in resp.text
    assert "btn-action" in resp.text


async def test_assess_form_uses_design_system_classes(client):
    resp = await client.get("/assess")
    assert "form-narrow" in resp.text
    assert "form-group" in resp.text
    assert "form-label" in resp.text
    assert "htmx-indicator" in resp.text


async def test_gates_uses_design_system_classes(client, _override_store):
    store = _override_store
    report = _make_report()
    aid = await store.save(report)
    await store.create_gate(aid, "cluster-admin-review", "Gate test")
    resp = await client.get("/admin-review")
    assert "gate-actions" in resp.text
    assert "btn-approve" in resp.text
    assert "btn-danger-outline" in resp.text
    assert "section-title" in resp.text


async def test_onboard_results_uses_design_system_classes(client, _override_store):
    store = _override_store
    report = _make_report_with_findings()
    aid = await store.save(report)
    await client.post(f"/assessments/{aid}/onboard", follow_redirects=False)
    resp = await client.get(f"/assessments/{aid}/onboard-results")
    assert "manifest-card" in resp.text
    assert "manifest-title" in resp.text
    assert "manifest-desc" in resp.text
    assert "code-block" in resp.text
    assert "delivery-actions" in resp.text
    assert "delivery-step" in resp.text
    assert "delivery-connector" in resp.text
    assert 'hx-boost="false"' in resp.text


async def test_responsive_css_exists(client):
    resp = await client.get("/")
    assert "@media (max-width: 768px)" in resp.text


# ── Agents page ────────────────────────────────────────────────────────


async def test_agents_page_empty(client, _override_store):
    """With no registered agents, watcher agents are still shown."""
    resp = await client.get("/agents")
    assert resp.status_code == 200
    assert "Agent Registry" in resp.text
    assert "vuln-watcher" in resp.text
    assert "slo-tracker" in resp.text
    assert "drift-detector" in resp.text


async def test_agents_page_with_data(client, _override_store):
    store = _override_store
    await store.register_agent("security", "hardening", "network,rbac")
    await store.register_agent("observability", "monitoring")
    resp = await client.get("/agents")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "observability" in resp.text
    assert "hardening" in resp.text


async def test_agents_page_last_heartbeat_stat_card_uses_data_timestamp(client, _override_store):
    """Regression: the stat card rendered a raw ISO timestamp while the
    identical data one row below (in the table's own "Last Heartbeat"
    column) correctly rendered as relative time -- both must use the same
    `data-timestamp` mechanism."""
    store = _override_store
    # Every long-lived watcher gets a real heartbeat so none fall back to
    # the synthetic "—" placeholder agents_page() merges in for watchers
    # missing from the registry (a bare em-dash would otherwise win
    # `max()` over any real ISO timestamp string).
    for name in ("vuln-watcher", "slo-tracker", "drift-detector", "skill-learner", "capability-scout"):
        await store.register_agent(name, "watcher")
        await store.agent_heartbeat(name)

    resp = await client.get("/agents")
    assert resp.status_code == 200
    stat_card = resp.text.split('<div class="stat-label">Last Heartbeat</div>', 1)[1][:400]
    assert "data-timestamp=" in stat_card
    assert "&mdash;" not in stat_card


async def test_agents_and_capabilities_are_tabs_of_each_other(client, _override_store):
    """Agents/Capabilities were split top-level nav items; now share one nav
    entry with a tab strip cross-linking Registry (agents) and Catalog
    (capabilities)."""
    agents_resp = await client.get("/agents")
    assert 'href="/capabilities"' in agents_resp.text

    capabilities_resp = await client.get("/capabilities")
    assert 'href="/agents"' in capabilities_resp.text


async def test_api_agents(client, _override_store):
    store = _override_store
    await store.register_agent("cicd", "deployment")
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert any(a["agent_name"] == "cicd" for a in data)


async def test_agent_detail_page(client, _override_store):
    store = _override_store
    await store.register_agent("security", "hardening", "network,rbac")
    await store.log_event("security", "completed", "test-app", "info", "Generated 5 files")
    report = _make_report()
    aid = await store.save(report)
    await store.save_remediation(aid, "security", "Add NetworkPolicy")
    resp = await client.get("/agents/security")
    assert resp.status_code == 200
    assert "security" in resp.text
    assert "hardening" in resp.text
    assert "Generated 5 files" in resp.text
    assert "Add NetworkPolicy" in resp.text


async def test_agent_detail_not_found(client, _override_store):
    resp = await client.get("/agents/nonexistent")
    assert resp.status_code == 404


async def test_agent_detail_shows_run_history(client, _override_store):
    store = _override_store
    await store.register_agent("security", "hardening", "network,rbac")
    await store.save_agent_run("security", "local", "success", duration_ms=1500, resource_tier="standard")
    await store.save_agent_run("security", "local", "error", duration_ms=200, error="boom")
    resp = await client.get("/agents/security")
    assert resp.status_code == 200
    assert "Run History" in resp.text
    assert "boom" in resp.text


async def test_agents_page_links_to_detail(client, _override_store):
    store = _override_store
    await store.register_agent("observability", "monitoring")
    resp = await client.get("/agents")
    assert resp.status_code == 200
    assert 'href="/agents/observability"' in resp.text


# ── Remediations page ─────────────────────────────────────────────────


async def test_remediations_page(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_remediation(aid, "security", "Fix RBAC")
    await store.save_remediation(aid, "observability", "Add metrics")
    resp = await client.get(f"/assessments/{aid}/remediations")
    assert resp.status_code == 200
    assert "Remediations" in resp.text
    assert "Fix RBAC" in resp.text
    assert "Add metrics" in resp.text


async def test_remediations_page_empty(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.get(f"/assessments/{aid}/remediations")
    assert resp.status_code == 200
    assert "No remediations" in resp.text


async def test_complete_remediation(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    rid = await store.save_remediation(aid, "security", "Fix RBAC")
    resp = await client.post(f"/assessments/{aid}/remediations/{rid}/complete", follow_redirects=False)
    assert resp.status_code == 303
    rems = await store.list_remediations(aid)
    assert rems[0]["status"] == "completed"


async def test_api_remediations(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_remediation(aid, "compliance", "Add SBOM")
    resp = await client.get(f"/api/assessments/{aid}/remediations")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── SLOs page ──────────────────────────────────────────────────────────


async def test_slos_page(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_slo(aid, "availability", 99.9)
    await store.save_slo(aid, "error_rate", 0.1)
    resp = await client.get(f"/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert "SLOs" in resp.text
    assert "availability" in resp.text
    assert "error_rate" in resp.text


async def test_slos_page_empty(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.get(f"/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert "No SLOs defined" in resp.text


async def test_api_slos(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_slo(aid, "latency_p99", 200.0)
    resp = await client.get(f"/api/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── Nav bar ────────────────────────────────────────────────────────────


async def test_nav_includes_agents_link(client):
    """/agents is no longer a top-level nav item — it's reachable via the
    Capabilities tab strip. The umbrella "Capabilities" link still appears
    globally, and /agents itself links back to /capabilities."""
    resp = await client.get("/")
    assert 'href="/capabilities"' in resp.text

    agents_resp = await client.get("/agents")
    assert 'href="/capabilities"' in agents_resp.text


# ── Assessment detail shows remediation/SLO buttons ────────────────────


async def test_assessment_detail_shows_remediation_button(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_remediation(aid, "security", "Fix it")
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/remediations" in resp.text
    assert "Remediations (1)" in resp.text


async def test_assessment_detail_renders_score_history(client, _override_store):
    """Regression: score_history was fetched and passed to the template but
    never rendered — the Overview tab now shows a history table with deltas."""
    store = _override_store
    first = _make_report("history-repo")
    first.scores[0].score = 40
    await store.save(first)
    second = _make_report("history-repo")
    second.scores[0].score = 70
    aid2 = await store.save(second)

    resp = await client.get(f"/assessments/{aid2}")
    assert resp.status_code == 200
    assert "Score History" in resp.text
    assert "score-history-table" in resp.text
    # No new inline styles introduced by the score-history feature.
    history_section = resp.text.split('<table class="score-history-table">')[1].split("</table>")[0]
    assert "style=" not in history_section


async def test_assessment_detail_shows_links_with_zero_counts(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert "Remediations (0)" in resp.text
    assert "SLOs (0)" in resp.text
    assert "History (0)" in resp.text


# ── SLO add form ───────────────────────────────────────────────────────


async def test_add_slo_via_form(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.post(
        f"/assessments/{aid}/slos/add",
        data={"metric_name": "availability", "target_value": "99.9"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    slos = await store.list_slos(aid)
    assert len(slos) == 1
    assert slos[0]["metric_name"] == "availability"
    assert slos[0]["target_value"] == 99.9


async def test_slos_page_shows_add_form(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.get(f"/assessments/{aid}/slos")
    assert resp.status_code == 200
    assert "Add SLO" in resp.text
    assert "metric_name" in resp.text
    assert "target_value" in resp.text


# ── Onboarding history ─────────────────────────────────────────────────


async def test_onboarding_history_empty(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.get(f"/assessments/{aid}/onboarding-history")
    assert resp.status_code == 200
    assert "No onboarding runs" in resp.text


async def test_onboarding_history_with_data(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_onboarding(aid, [
        {"category": "security", "path": "rbac.yaml", "content": "kind: Role", "description": "rbac"},
    ], orchestration={"recommendation": "READY FOR REVIEW", "auto_approve": False})
    resp = await client.get(f"/assessments/{aid}/onboarding-history")
    assert resp.status_code == 200
    assert "READY FOR REVIEW" in resp.text
    assert "1" in resp.text  # file count


async def test_assessment_detail_shows_history_button(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_onboarding(aid, [{"category": "c", "path": "f.yaml", "content": "x", "description": "d"}])
    resp = await client.get(f"/assessments/{aid}")
    assert resp.status_code == 200
    assert f"/assessments/{aid}/onboarding-history" in resp.text
    assert "History (1)" in resp.text


# ── Settings page ──────────────────────────────────────────────────────


async def test_settings_page_default(client, _override_store):
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    assert "Auto-Mode" in resp.text
    assert "OFF" in resp.text


async def test_toggle_auto_mode_on(client, _override_store):
    store = _override_store
    resp = await client.post("/settings/auto-mode", data={"value": "true"}, follow_redirects=False)
    assert resp.status_code == 303
    assert await store.get_setting("auto_mode") == "true"


async def test_toggle_auto_mode_off(client, _override_store):
    store = _override_store
    await store.set_setting("auto_mode", "true")
    resp = await client.post("/settings/auto-mode", data={"value": "false"}, follow_redirects=False)
    assert resp.status_code == 303
    assert await store.get_setting("auto_mode") == "false"


async def test_settings_page_shows_on_when_enabled(client, _override_store):
    store = _override_store
    await store.set_setting("auto_mode", "true")
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "ON" in resp.text
    assert "Disable Global Fallback" in resp.text


async def test_settings_nav_link(client):
    resp = await client.get("/")
    assert 'href="/settings"' in resp.text


async def test_settings_auto_mode_banner_does_not_reference_retired_gates_page(client, _override_store):
    """Regression: the Auto-Mode banner said destructive changes get
    "queued in Gates for your review" -- Gates no longer exists as a
    standalone page (split into per-app Actions tabs + Admin Review)."""
    store = _override_store
    await store.set_setting("auto_mode", "true")
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "queued in Gates" not in resp.text
    assert "Actions tab" in resp.text
    assert "Admin Review" in resp.text


async def test_settings_and_schedules_are_tabs_of_each_other(client, _override_store):
    """Settings/Schedules were split top-level nav items; now share one nav
    entry with a tab strip cross-linking the two pages."""
    settings_resp = await client.get("/settings")
    assert '/settings"' in settings_resp.text
    assert 'href="/schedules"' in settings_resp.text

    schedules_resp = await client.get("/schedules")
    assert 'href="/settings"' in schedules_resp.text


# ── Auto-mode allowlist (Settings page) ───────────────────────────────


async def test_settings_page_shows_allowlist_empty_by_default(client, _override_store):
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "Auto-Mode Allowlist" in resp.text
    assert "No allowlist entries configured" in resp.text


async def test_add_auto_mode_allowlist_entry(client, _override_store):
    store = _override_store
    resp = await client.post(
        "/settings/auto-mode-allowlist/add",
        data={"namespace": "prod", "kind": "ConfigMap"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from agentit.automode import parse_allowlist
    assert parse_allowlist(await store.get_setting("auto_mode_allowlist")) == ["prod/ConfigMap"]


async def test_add_auto_mode_allowlist_entry_defaults_namespace_to_wildcard(client, _override_store):
    store = _override_store
    await client.post("/settings/auto-mode-allowlist/add", data={"kind": "NetworkPolicy"}, follow_redirects=False)
    from agentit.automode import parse_allowlist
    assert parse_allowlist(await store.get_setting("auto_mode_allowlist")) == ["*/NetworkPolicy"]


async def test_add_auto_mode_allowlist_rejects_rbac_shaped_kind(client, _override_store):
    """The Settings page rejects an RBAC-shaped kind up front with a clear
    error, rather than silently accepting a pattern that `split_files_by_
    allowlist()` would ignore anyway."""
    store = _override_store
    resp = await client.post(
        "/settings/auto-mode-allowlist/add",
        data={"namespace": "*", "kind": "Secret"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "allowlist_error" in resp.headers["location"]
    from agentit.automode import parse_allowlist
    assert parse_allowlist(await store.get_setting("auto_mode_allowlist")) == []


async def test_remove_auto_mode_allowlist_entry(client, _override_store):
    store = _override_store
    await client.post(
        "/settings/auto-mode-allowlist/add", data={"namespace": "prod", "kind": "ConfigMap"},
        follow_redirects=False,
    )
    resp = await client.post(
        "/settings/auto-mode-allowlist/remove", data={"pattern": "prod/ConfigMap"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    from agentit.automode import parse_allowlist
    assert parse_allowlist(await store.get_setting("auto_mode_allowlist")) == []


async def test_settings_page_lists_configured_allowlist_entries(client, _override_store):
    store = _override_store
    await store.set_setting("auto_mode_allowlist", '["prod/ConfigMap", "*/NetworkPolicy"]')
    resp = await client.get("/settings")
    assert "prod" in resp.text
    assert "ConfigMap" in resp.text
    assert "NetworkPolicy" in resp.text


# ── Schedules page ─────────────────────────────────────────────────────


async def test_schedules_page_empty(client, _override_store):
    resp = await client.get("/schedules")
    assert resp.status_code == 200
    assert "Scheduled Operations" in resp.text


async def test_schedules_page_shows_watchers(client, _override_store):
    resp = await client.get("/schedules")
    assert resp.status_code == 200
    assert "vuln-watcher" in resp.text
    assert "slo-tracker" in resp.text
    assert "drift-detector" in resp.text


async def test_schedules_app_name_links_to_assessment_for_manual_schedule(client, _override_store):
    """Every other page (Fleet, Remediations, Decisions) links an app name
    to its Assessment Detail page -- Schedules showed plain text instead."""
    store = _override_store
    aid = await store.save(_make_report("scheduled-app"))
    await store.create_schedule("scheduled-app", "Nightly scan", "compliance", "0 3 * * *", "cmd")

    resp = await client.get("/schedules")
    assert resp.status_code == 200
    assert f'<a href="/assessments/{aid}">scheduled-app</a>' in resp.text


async def test_schedules_app_name_plain_text_when_no_assessment_resolves(client, _override_store):
    """A manual schedule's app_name is free text -- never fabricate a link
    target when no matching assessment exists."""
    store = _override_store
    await store.create_schedule("no-such-app", "Nightly scan", "compliance", "0 3 * * *", "cmd")

    resp = await client.get("/schedules")
    assert resp.status_code == 200
    assert "no-such-app" in resp.text
    assert 'href="/assessments/' not in resp.text


async def test_schedules_nav_link(client):
    """/schedules is no longer a top-level nav item — it's reachable via the
    Settings tab strip. The umbrella "Settings" link still appears globally."""
    resp = await client.get("/")
    assert 'href="/settings"' in resp.text


async def test_update_schedule(client, _override_store):
    store = _override_store
    resp = await client.post("/schedules/update", data={
        "app_name": "test-app",
        "job_key": "compliance",
        "schedule": "0 6 1 * *",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert await store.get_setting("schedule:test-app:compliance") == "0 6 1 * *"


async def test_toggle_schedule(client, _override_store):
    store = _override_store
    resp = await client.post("/schedules/toggle", data={
        "app_name": "test-app",
        "job_key": "chaos",
        "enabled": "false",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert await store.get_setting("schedule:test-app:chaos:enabled") == "false"


# ── All pages accessible ──────────────────────────────────────────────


async def test_all_pages_return_200(client, _override_store):
    """Smoke test: every page returns 200."""
    store = _override_store
    aid = await store.save(_make_report())
    await store.register_agent("security", "hardening")

    pages = [
        "/",
        "/assess",
        "/events",
        "/gates",
        "/admin-review",
        "/agents",
        "/schedules",
        "/settings",
        f"/assessments/{aid}",
    ]
    for page in pages:
        resp = await client.get(page)
        assert resp.status_code == 200, f"{page} returned {resp.status_code}"


# ── Health page ────────────────────────────────────────────────────────


async def test_health_page(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert "System Health" in resp.text


async def test_health_api(client):
    resp = await client.get("/api/health")
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "pods_running" in data
    assert "pipeline_status" in data


async def test_health_nav_link(client):
    resp = await client.get("/")
    assert 'href="/health"' in resp.text


async def test_failed_taskrun_pod_excluded_from_platform_degraded(client):
    """A Tekton TaskRun-owned pod that ended up Failed (e.g. a one-off
    onboarding/build attempt against a bad branch) is a terminal, one-shot
    execution record -- not an ongoing service health signal -- so it must
    not pin `pods_failed` (and the Health page's "Platform" card) at
    Degraded forever. A Failed pod NOT owned by a TaskRun (a real crashing
    service pod) must still count. Regression test for a live incident: a
    stale `*-git-clone-pod` kept /health reporting Degraded for hours after
    the onboarding attempt itself had already finished."""
    taskrun_pod = {
        "name": "build-hello-world-25952-git-clone-pod", "status": "Failed",
        "restarts": 0, "age": "2026-07-15T14", "owner_kind": "TaskRun",
    }
    real_failure_pod = {
        "name": "agentit-worker-abc123", "status": "Failed",
        "restarts": 3, "age": "2026-07-15T17", "owner_kind": "ReplicaSet",
    }
    # `_get_cluster_health` does a local `from agentit import kube`, which
    # shadows any patch on `agentit.portal.routes.health.kube` -- patch the
    # real `agentit.kube` functions it actually resolves to instead.
    with patch("agentit.kube.list_pods") as mock_list_pods, \
            patch("agentit.kube.list_custom_resources") as mock_list_crs:
        mock_list_pods.return_value = [taskrun_pod, real_failure_pod]
        mock_list_crs.return_value = []
        resp = await client.get("/api/health")

    data = resp.json()
    assert data["pods_failed"] == 1
    assert resp.status_code == 503


async def test_failed_non_taskrun_pod_only_excluded_when_owned_by_taskrun(client):
    """Sanity check the inverse: with only the TaskRun-owned failed pod
    present (no other failures), pods_failed is 0 and /api/health is
    healthy on that signal."""
    taskrun_pod = {
        "name": "build-hello-world-25952-git-clone-pod", "status": "Failed",
        "restarts": 0, "age": "2026-07-15T14", "owner_kind": "TaskRun",
    }
    with patch("agentit.kube.list_pods") as mock_list_pods, \
            patch("agentit.kube.list_custom_resources") as mock_list_crs:
        mock_list_pods.return_value = [taskrun_pod]
        mock_list_crs.return_value = []
        resp = await client.get("/api/health")

    data = resp.json()
    assert data["pods_failed"] == 0


async def test_pod_detail_404(client):
    """Mock the kube client so this test is hermetic (no live-cluster round trip)."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.core_v1.return_value.read_namespaced_pod.side_effect = Exception("not found")
        resp = await client.get("/health/pods/nonexistent-pod-xyz")
    assert resp.status_code == 404


async def test_pipeline_detail_404(client):
    """Mock the kube client so this test is hermetic (no live-cluster round trip)."""
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.get_custom_resource.return_value = None
        resp = await client.get("/health/pipelines/nonexistent-run-xyz")
    assert resp.status_code == 404


async def test_pod_detail_success(client):
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

        resp = await client.get("/health/pods/my-pod")

    assert resp.status_code == 200
    assert "Running" in resp.text
    assert "log line 1" in resp.text
    assert "BackOff" in resp.text
    mock_kube.core_v1.return_value.read_namespaced_pod.assert_called_with(
        "my-pod", "agentit", _request_timeout=10,
    )


async def test_pipeline_detail_success(client):
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

        resp = await client.get("/health/pipelines/build-my-app-123")

    assert resp.status_code == 200
    assert "Succeeded" in resp.text
    assert "git-clone" in resp.text
    mock_kube.get_custom_resource.assert_called_with(
        "tekton.dev", "v1", "pipelineruns", "build-my-app-123", namespace="agentit",
    )


async def test_operator_status_installed(client):
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
        resp = await client.get("/api/operator-status?package=my-operator")

    assert resp.status_code == 200
    assert "Installed" in resp.text
    mock_kube.list_custom_resources.assert_called_with(
        "operators.coreos.com", "v1alpha1", "clusterserviceversions", "openshift-my-operator",
    )


async def test_operator_status_still_installing(client):
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.return_value = []
        mock_kube.get_custom_resource.return_value = {"status": {"state": "AtLatestKnown"}}
        resp = await client.get("/api/operator-status?package=my-operator")

    assert resp.status_code == 200
    assert "AtLatestKnown" in resp.text


async def test_operator_status_escapes_reflected_package_param(client):
    """Regression test for docs/code-review-2026-07-12.md item #1: `package`
    is a client-supplied query param interpolated into a raw HTMLResponse
    (bypassing Jinja2 autoescaping), so an unescaped value is reflected XSS."""
    payload = '<script>alert(1)</script>'
    with patch("agentit.portal.routes.health.kube") as mock_kube:
        mock_kube.list_custom_resources.side_effect = Exception("cluster unreachable")
        resp = await client.get(f"/api/operator-status?package={payload}")

    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


# ── Gate deduplication and expiry ──────────────────────────────────────


async def test_gate_deduplication(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    g1 = await store.create_gate(aid, "deploy", "First gate")
    g2 = await store.create_gate(aid, "deploy", "Duplicate gate")
    assert g1 == g2  # same gate returned


async def test_gate_different_types_not_deduped(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    g1 = await store.create_gate(aid, "deploy", "Deploy gate")
    g2 = await store.create_gate(aid, "security-review", "Security gate")
    assert g1 != g2


async def test_stale_gate_expiry(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    await store.create_gate(aid, "deploy", "Old gate")
    # Manually backdate the gate
    await store._pool.execute(
        "UPDATE gates SET created_at = '2020-01-01T00:00:00Z' WHERE status = 'pending'"
    )
    expired = await store.expire_stale_gates(hours=1)
    assert expired == 1
    assert len(await store.list_gates("pending")) == 0


# ── Delete ─────────────────────────────────────────────────────────────


async def test_delete_assessment_route(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    resp = await client.post(f"/assessments/{aid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert await store.get(aid) is None


async def test_delete_cascades(client, _override_store):
    """Delete removes all related data — remediations, SLOs, gates, onboarding."""
    store = _override_store
    aid = await store.save(_make_report())
    await store.save_remediation(aid, "security", "Fix RBAC")
    await store.save_slo(aid, "availability", 99.9)
    await store.create_gate(aid, "deploy", "Approve deploy")
    await store.save_onboarding(aid, [{"category": "sec", "path": "x.yaml", "content": "y", "description": "d"}])

    resp = await client.post(f"/assessments/{aid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert await store.get(aid) is None
    assert await store.list_remediations(aid) == []
    assert await store.list_slos(aid) == []
    assert await store.get_onboarding(aid) is None


async def test_delete_nonexistent_returns_404(client, _override_store):
    resp = await client.post("/assessments/fake-id/delete", follow_redirects=False)
    assert resp.status_code == 404


async def test_delete_slo_route(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    sid = await store.save_slo(aid, "latency", 200.0)
    resp = await client.post(f"/assessments/{aid}/slos/{sid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert len(await store.list_slos(aid)) == 0


async def test_delete_remediation_route(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    rid = await store.save_remediation(aid, "cicd", "Add pipeline")
    resp = await client.post(f"/assessments/{aid}/remediations/{rid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert len(await store.list_remediations(aid)) == 0


async def test_cancel_gate_route(client, _override_store):
    store = _override_store
    aid = await store.save(_make_report())
    gid = await store.create_gate(aid, "deploy", "Approve")
    resp = await client.post(f"/gates/{gid}/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert len(await store.list_gates("pending")) == 0


# ── Capabilities: learn (research CVEs & generate skills) ──────────────


async def test_capabilities_page_has_learn_button(client):
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert '/capabilities/learn' in resp.text
    assert "Research CVEs" in resp.text


async def test_capabilities_learn_button_uses_toast_not_verbose_label(client):
    """The "can take up to 3 minutes, please don't close this tab" caveat
    belongs in a dismissible toast fired on click (showToast(...) in the
    button's @click), not crammed into the button's own visible loading-
    state label -- that stays a short "Researching..." regardless of state."""
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "showToast(" in resp.text
    assert "can take up to 3 minutes" in resp.text  # present, but inside the toast trigger below
    indicator = resp.text.split('class="htmx-indicator spinner-wrap"', 1)[1][:150]
    assert "can take up to 3 minutes" not in indicator
    assert "Researching" in indicator


async def test_capabilities_catalog_collapses_use_buttons(client):
    """New Capabilities collapses are real <button> toggles (keyboard /
    AT), not clickable <h2> headings."""
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert 'type="button" class="section-title collapse-toggle"' in resp.text
    assert resp.text.count('type="button" class="section-title collapse-toggle"') >= 3


async def test_capabilities_learn_without_llm_shows_error(client, _override_store):
    with patch("agentit.portal.routes.capabilities.get_llm_client", return_value=None):
        resp = await client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "LLM" in resp.headers["location"]
    # Even a run that never reaches the LLM must leave a durable trace --
    # that's the whole point of the "learning-run" action (Bucket 1 of the
    # learn-button transparency work: every attempt is queryable later, not
    # just the ones that generated a skill).
    events = await _override_store.list_events_by_action("learning-run", limit=5)
    assert len(events) == 1
    assert events[0]["severity"] == "error"
    assert events[0]["agent_id"] == "learning-agent"


async def test_capabilities_learn_generates_new_skill(client, _override_store):
    with patch("agentit.portal.routes.capabilities.get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00001"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=False), \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: cve-2099-00001\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/cve-2099-00001.md")):
        resp = await client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "success=" in resp.headers["location"]
    events = await _override_store.list_events_by_agent("learning-agent", limit=5)
    assert any(e["action"] == "skills-generated" for e in events)
    run_events = await _override_store.list_events_by_action("learning-run", limit=5)
    assert len(run_events) == 1
    assert run_events[0]["severity"] == "info"
    assert "cve-2099-00001" in run_events[0]["summary"]


async def test_capabilities_learn_skips_existing_skill(client, _override_store):
    with patch("agentit.portal.routes.capabilities.get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00002"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=True):
        resp = await client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    # A no-op run isn't the same as success -- it gets its own (non-error)
    # "warning" toast so it isn't mistaken for "a skill was generated".
    assert "warning=" in resp.headers["location"]
    events = await _override_store.list_events_by_agent("learning-agent", limit=5)
    assert not any(e["action"] == "skills-generated" for e in events)
    run_events = await _override_store.list_events_by_action("learning-run", limit=5)
    assert len(run_events) == 1
    assert run_events[0]["severity"] == "warning"


async def test_capabilities_learn_no_research_results(client, _override_store):
    with patch("agentit.portal.routes.capabilities.get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[]):
        resp = await client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "warning=" in resp.headers["location"]
    run_events = await _override_store.list_events_by_action("learning-run", limit=5)
    assert len(run_events) == 1
    assert run_events[0]["severity"] == "warning"


async def test_capabilities_learn_research_failure_logs_error_event(client, _override_store):
    """The exception path must also leave a durable trace, not just a
    transient toast -- this is the "not just successful ones" half of the
    every-run-leaves-a-trace requirement."""
    with patch("agentit.portal.routes.capabilities.get_llm_client", return_value=object()), \
         patch("agentit.learning_agent.research_cves", side_effect=RuntimeError("LLM timed out")):
        resp = await client.post("/capabilities/learn", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    run_events = await _override_store.list_events_by_action("learning-run", limit=5)
    assert len(run_events) == 1
    assert run_events[0]["severity"] == "error"
    assert "LLM timed out" in run_events[0]["summary"]


async def test_capabilities_page_shows_flagged_skill_preview(client, _override_store):
    """Item 4: the button's description should say what it's about to do,
    not just what it already did -- if a skill is flagged low-effectiveness,
    the preview must name it instead of the generic CVE-sweep description."""
    for _ in range(6):
        await _override_store.record_skill_outcome("network-policy", "app-a", "rejected", "wrong")
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "improve" in resp.text
    assert "network-policy" in resp.text


async def test_capabilities_page_shows_cve_sweep_preview_when_nothing_flagged(client):
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "No skills are currently flagged as low-effectiveness" in resp.text


async def test_capabilities_page_shows_learning_run_history(client, _override_store):
    """Item 1: a past run (of either trigger source) must show up in a
    visible history table on the page, not just in the ephemeral toast."""
    await _override_store.log_event(
        "learning-agent", "learning-run", None, "warning",
        "No new skills — 1 researched CVE(s) already have matching skills.",
        details={"trigger": "manual", "mode": "cve-sweep", "saved": [], "skipped": ["CVE-2099-00099"]},
    )
    await _override_store.log_event(
        "skill-learner", "learning-run", None, "info",
        "Generated 1 improvement(s): network-policy-v2",
        details={"trigger": "watcher", "mode": "skill-improvement", "saved": ["network-policy-v2"], "skipped": []},
    )
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "Learning Agent Runs" in resp.text
    assert "already have matching skills" in resp.text
    assert "Automatic (24h watcher)" in resp.text
    assert "Manual" in resp.text


async def test_capabilities_page_shows_skill_learner_never_ticked(client):
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "never ticked on this deployment" in resp.text


async def test_capabilities_page_shows_skill_learner_recent_heartbeat(client, _override_store):
    await _override_store.agent_heartbeat("skill-learner")
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "is running" in resp.text


# ── Capabilities: watcher-submitted skill drafts (cross-pod visibility) ──
#
# The skill-learner watcher runs in its own pod with no shared filesystem
# with the portal (no RWX storage class is available on this cluster --
# see chart/templates/agents/skill-learner.yaml's comments). It now pushes
# every draft to this internal endpoint instead of only writing to its own
# isolated disk -- these tests prove the draft becomes visible via the
# portal's OWN skill-listing logic (skill_engine.load_all_skills(), what
# _cached_skills() calls), not just that a file landed somewhere.


async def test_webhook_skill_draft_requires_content(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    resp = await client.post("/api/webhook/skill-draft", json={"domain": "security"})
    assert resp.status_code == 400


async def test_webhook_skill_draft_saves_and_busts_cache(client, tmp_path, monkeypatch):
    """The core cross-pod-visibility fix: a draft submitted through this
    internal endpoint (what the skill-learner watcher's own pod calls
    instead of writing only to its own isolated filesystem) lands exactly
    where `_cached_skills()` -- the same function backing the Capabilities
    page -- looks, and is visible with no portal restart or manual sync."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()

    from agentit.portal.routes import capabilities as capabilities_routes
    capabilities_routes._skills_cache["data"] = ["stale-cached-list"]

    content = (
        "---\nname: watcher-drafted-cve\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\nstatus: draft\n---\nbody\n"
    )
    resp = await client.post("/api/webhook/skill-draft", json={"content": content, "domain": "security"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "watcher-drafted-cve"
    assert (tmp_path / "skills" / "security" / "watcher-drafted-cve.md").exists()

    # Prove visibility via the portal's OWN skill-listing logic, not just
    # "the file exists on disk somewhere".
    from agentit.skill_engine import load_all_skills
    skills = load_all_skills(tmp_path / "skills")
    assert any(s.name == "watcher-drafted-cve" and s.status == "draft" for s in skills)

    # And the 60s Capabilities cache must be busted -- the exact same cache
    # capabilities_learn_route busts after a manual button-triggered draft --
    # so the next page load reflects it with no restart or manual step.
    assert capabilities_routes._skills_cache["data"] is None


async def test_webhook_skill_draft_save_failure_returns_500(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    with patch("agentit.learning_agent.save_skill", return_value=None):
        resp = await client.post(
            "/api/webhook/skill-draft",
            json={"content": "---\nname: x\n---\nbody", "domain": "security"},
        )
    assert resp.status_code == 500


# ── Capabilities: activate a draft skill ────────────────────────────────


def _make_draft_skill(tmp_path, name="cve-2099-00003", domain="security") -> Path:
    """A draft skill that actually generates valid output -- since
    activate_skill_route now runs verify_skill() (functional generation
    smoke test) before flipping status, a body with no usable template
    would legitimately fail activation."""
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
        "## Property\nEnsures network isolation.\n\n"
        "## Constraints\nMust apply to all pods.\n\n"
        "## Verification\nCheck that a NetworkPolicy restricting Ingress exists.\n\n"
        "```yaml\n"
        "apiVersion: networking.k8s.io/v1\n"
        "kind: NetworkPolicy\n"
        "metadata:\n"
        "  name: {{app_name}}-netpol\n"
        "spec:\n"
        "  podSelector: {}\n"
        "  policyTypes:\n"
        "    - Ingress\n"
        "```\n",
        encoding="utf-8",
    )
    return skill_file


async def test_activate_skill_promotes_draft_to_active(client, tmp_path, monkeypatch):
    skill_file = _make_draft_skill(tmp_path)
    monkeypatch.chdir(tmp_path)

    resp = await client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(skill_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "success=" in resp.headers["location"]
    assert "status: active" in skill_file.read_text()


async def test_activate_skill_already_active_shows_error(client, tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills" / "security"
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / "already-active.md"
    skill_file.write_text(
        "---\nname: already-active\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\nstatus: active\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resp = await client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(skill_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


async def test_activate_skill_rejects_path_outside_skills_dir(client, tmp_path, monkeypatch):
    outside_file = tmp_path / "not-a-skill.md"
    outside_file.write_text("status: draft", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    monkeypatch.chdir(tmp_path)

    resp = await client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(outside_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert outside_file.read_text() == "status: draft"


async def test_activate_skill_missing_file_shows_error(client, tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    monkeypatch.chdir(tmp_path)

    resp = await client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(tmp_path / "skills" / "nope.md")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


async def test_activate_skill_blocked_when_generation_fails(client, tmp_path, monkeypatch):
    """A draft skill with no usable template and no LLM fails verify_skill()'s
    functional check -- activation must be blocked, not silently allowed."""
    skills_dir = tmp_path / "skills" / "security"
    skills_dir.mkdir(parents=True)
    skill_file = skills_dir / "nonfunctional.md"
    skill_file.write_text(
        "---\nname: nonfunctional\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\nstatus: draft\n---\nno template here\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resp = await client.post(
        "/capabilities/skills/activate",
        data={"skill_path": str(skill_file)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "status: draft" in skill_file.read_text()


# ── Capabilities: per-skill lifecycle view ──────────────────────────────


async def test_skill_history_page_renders_for_unknown_skill(client):
    """A skill with no recorded outcomes/events still renders a valid page
    (not a 404/500) -- most skills won't have effectiveness data yet."""
    resp = await client.get("/capabilities/skills/some-skill-with-no-history/history")
    assert resp.status_code == 200
    assert "some-skill-with-no-history" in resp.text
    assert "No recorded outcomes" in resp.text


async def test_skill_history_page_shows_outcomes_and_events(client, _override_store):
    store = _override_store
    await store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
    await store.record_skill_outcome("network-policy", "app-b", "rejected", "wrong port")
    await store.log_event("skill-inventory", "skill-added", None, "info", "New skill added: security/network-policy")

    resp = await client.get("/capabilities/skills/network-policy/history")
    assert resp.status_code == 200
    assert "app-a" in resp.text
    assert "app-b" in resp.text
    assert "looks fine" in resp.text
    assert "New skill added: security/network-policy" in resp.text


async def test_get_skill_history_store_method():
    from conftest import make_store
    store = await make_store()
    await store.record_skill_outcome("rbac", "app-a", "approved", "fine")
    await store.log_event("drift-detector", "skill-deprecated", "cluster", "warning",
                     "Auto-deprecated skill rbac: RoleBinding API removed")

    history = await store.get_skill_history("rbac")
    assert len(history["outcomes"]) == 1
    assert history["outcomes"][0]["app_name"] == "app-a"
    assert len(history["events"]) == 1
    assert "Auto-deprecated skill rbac" in history["events"][0]["summary"]


# ── Capabilities: catalog change tracking (skill_inventory) ────────────


async def test_capabilities_page_renders_catalog_changes_section(client):
    """The 'Recent Catalog Changes' section should always render, even empty."""
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "Recent Catalog Changes" in resp.text


async def test_capabilities_page_shows_catalog_change_events(client, _override_store):
    store = _override_store
    await store.log_event("skill-inventory", "skill-added", None, "info",
                     "New skill added: security/cve-2099-1")
    await store.log_event("skill-inventory", "check-removed", None, "warning",
                     "Check removed: reliability/has-readiness-probe")

    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "New skill added: security/cve-2099-1" in resp.text
    assert "Check removed: reliability/has-readiness-probe" in resp.text
    assert "badge-success" in resp.text
    assert "badge-warning" in resp.text


async def test_background_skill_inventory_diff_surfaces_on_events_page(client, _override_store, tmp_path, monkeypatch):
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
    await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)
    assert await store.list_events_by_agent("skill-inventory") == []

    # A new skill lands on disk (as if a PR merged to skills/).
    (security_dir / "cve-2099-1.md").write_text(
        "---\nname: cve-2099-1\ndomain: security\nversion: 1\n"
        "triggers: [test]\noutputs: [NetworkPolicy]\n---\nbody\n",
        encoding="utf-8",
    )

    # Second tick: diff finds the addition and logs an event.
    await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)

    events = await store.list_events_by_agent("skill-inventory")
    assert len(events) == 1
    assert events[0]["action"] == "skill-added"

    resp = await client.get("/events")
    assert resp.status_code == 200
    assert "skill-added" in resp.text
    assert "cve-2099-1" in resp.text

    caps_resp = await client.get("/capabilities")
    assert caps_resp.status_code == 200
    assert "cve-2099-1" in caps_resp.text


# ── Capabilities: Self-Improvement tab (capability-scout) ──────────────
#
# docs/self-improvement-for-agentit.md's "Portal transparency" section --
# a human should be able to see every capability-scout cycle, its evidence,
# and its gate results entirely from inside the portal, without needing to
# already know a PR exists.


async def test_self_improvement_tab_renders_when_empty(client):
    resp = await client.get("/capabilities/self-improvement")
    assert resp.status_code == 200
    assert "Self-Improvement" in resp.text
    assert "never ticked on this deployment" in resp.text


async def test_self_improvement_tab_is_a_third_tab_alongside_catalog_and_registry(client):
    resp = await client.get("/capabilities/self-improvement")
    assert 'href="/capabilities"' in resp.text
    assert 'href="/agents"' in resp.text

    catalog_resp = await client.get("/capabilities")
    assert 'href="/capabilities/self-improvement"' in catalog_resp.text


async def test_self_improvement_tab_shows_run_history(client, _override_store):
    """Every cycle appears here, including ones that proposed nothing or
    got gate-blocked -- not just the ones that shipped a PR."""
    store = _override_store
    await store.log_event(
        "capability-scout", "capability-run", None, "warning",
        "No proposal this cycle — insufficient real signal (1 data point(s), need 5).",
        details={"trigger": "watcher", "evidence": "", "doc_anchor": None, "gate_results": [], "pr_url": None},
    )
    await store.log_event(
        "capability-scout", "capability-run", None, "warning",
        "Proposal 'Track stack signatures' gate-blocked: test-plan-required",
        details={
            "trigger": "watcher", "evidence": "README.md:42 — Documented future idea",
            "doc_anchor": "README.md:42",
            "gate_results": [{"name": "test-plan-required", "passed": False, "detail": "no test_plan"}],
            "pr_url": None,
        },
    )

    resp = await client.get("/capabilities/self-improvement")
    assert resp.status_code == 200
    assert "Self-Improvement Runs" in resp.text
    assert "insufficient real signal" in resp.text
    assert "gate-blocked" in resp.text
    assert "Automatic (24h watcher)" in resp.text


async def test_self_improvement_tab_shows_watcher_heartbeat(client, _override_store):
    await _override_store.agent_heartbeat("capability-scout")
    resp = await client.get("/capabilities/self-improvement")
    assert resp.status_code == 200
    assert "is running" in resp.text


async def test_capability_run_detail_page_renders_evidence_and_gates(client, _override_store):
    store = _override_store
    event_id = await store.log_event(
        "capability-scout", "capability-run", None, "warning",
        "Proposal 'Track stack signatures' gate-blocked: test-plan-required",
        details={
            "trigger": "watcher",
            "title": "Track stack signatures",
            "evidence": "README.md:42 — Documented future idea",
            "risk": "low",
            "doc_anchor": "README.md:42",
            "gate_results": [
                {"name": "diff-size", "passed": True, "detail": "1 file(s), 20 line(s) — within cap"},
                {"name": "test-plan-required", "passed": False, "detail": "proposal has no test_plan"},
            ],
            "pr_url": None,
        },
    )

    resp = await client.get(f"/capabilities/self-improvement/runs/{event_id}")
    assert resp.status_code == 200
    assert "README.md:42" in resp.text
    assert "test-plan-required" in resp.text
    assert "diff-size" in resp.text
    assert "low" in resp.text


async def test_capability_run_detail_page_404s_for_unknown_run(client, _override_store):
    resp = await client.get("/capabilities/self-improvement/runs/does-not-exist")
    assert resp.status_code == 404


async def test_capability_run_detail_shows_live_pr_status(client, _override_store):
    store = _override_store
    event_id = await store.log_event(
        "capability-scout", "capability-run", None, "info",
        "Opened proposal PR: Track stack signatures (https://github.com/org/agentit/pull/9)",
        details={
            "trigger": "watcher", "title": "Track stack signatures", "evidence": "e", "risk": "low",
            "gate_results": [{"name": "diff-size", "passed": True, "detail": "ok"}],
            "pr_url": "https://github.com/org/agentit/pull/9",
        },
    )

    with patch("agentit.portal.github_pr.get_pr_status", return_value={"state": "open", "html_url": "https://github.com/org/agentit/pull/9"}):
        resp = await client.get(f"/capabilities/self-improvement/runs/{event_id}")

    assert resp.status_code == 200
    assert "open" in resp.text
    assert "https://github.com/org/agentit/pull/9" in resp.text
    # External PR hrefs go through the safe_url filter (javascript: etc. → #).
    assert 'href="https://github.com/org/agentit/pull/9"' in resp.text
    assert 'href="{{ pr_url }}"' not in resp.text


async def test_ledger_pending_count_uses_badge_accent(client, _override_store):
    """Pending-action counts on Ledger rows are attention signals — same
    badge-accent as Fleet's Needs Action badge, not badge-warning."""
    store = _override_store
    aid = await store.save(_make_report("ledger-pending-badge"))
    await store.create_gate(aid, "auto-mode-review", "needs review")
    resp = await client.get("/ledger?needs_you=0")
    assert resp.status_code == 200
    assert 'class="badge badge-accent">1 pending</span>' in resp.text
    assert 'class="badge badge-warning">1 pending</span>' not in resp.text


# ── Agent registry cleanup (agent_registry_cleanup) ───────────────────


async def test_background_agent_registry_prune_surfaces_on_events_page(client, _override_store):
    """Simulates a tick of `_background_maintenance()`'s agent-registry-prune
    step without waiting for the real hourly loop, then confirms the pruned
    agents disappear from `/api/agents` and the resulting event shows up on
    `/events`."""
    from agentit.agent_registry_cleanup import prune_stale_agents_and_log

    store = _override_store
    # Rows left behind by Python agents removed in favor of skills-only
    # generation, plus the legitimate agents/watchers that must survive.
    await store.register_agent("security", "security")
    await store.register_agent("observability", "observability")
    await store.register_agent("cost", "cost")
    await store.agent_heartbeat("vuln-watcher")

    pruned = await prune_stale_agents_and_log(store)
    assert sorted(pruned) == ["observability", "security"]

    api_resp = await client.get("/api/agents")
    assert api_resp.status_code == 200
    names = {a["agent_name"] for a in api_resp.json()}
    assert names == {"cost", "vuln-watcher"}

    events_resp = await client.get("/events")
    assert events_resp.status_code == 200
    assert "agent-registry-pruned" in events_resp.text

    events = await store.list_events_by_agent("agent-registry")
    assert len(events) == 1
    assert events[0]["action"] == "agent-registry-pruned"
    assert "security" in events[0]["summary"]
    assert "observability" in events[0]["summary"]


async def test_background_agent_registry_prune_is_noop_when_nothing_stale(client, _override_store):
    """No stale rows -> no event, no change -- confirms the loop can run
    safely on every tick without spamming the Events feed."""
    from agentit.agent_registry_cleanup import prune_stale_agents_and_log

    store = _override_store
    await store.register_agent("cost", "cost")
    await store.register_agent("dependency", "dependency")

    pruned = await prune_stale_agents_and_log(store)

    assert pruned == []
    assert await store.list_events_by_agent("agent-registry") == []
    names = {a["agent_name"] for a in await store.list_agents()}
    assert names == {"cost", "dependency"}


# ── Loop health meta-metric ──────────────────────────────────────────────


async def test_get_loop_health_no_flagged_skills():
    from conftest import make_store
    store = await make_store()
    health = await store.get_loop_health()
    assert health["flagged_count"] == 0
    assert health["pct_with_improvement"] is None


async def test_get_loop_health_counts_recent_improvement_drafts():
    from conftest import make_store
    store = await make_store()
    for _ in range(5):
        await store.record_skill_outcome("network-policy", "app-a", "rejected", "wrong")
    for _ in range(5):
        await store.record_skill_outcome("containerfile", "app-a", "rejected", "wrong")

    # Only network-policy got a follow-up improvement draft.
    await store.log_event("skill-learner", "skill-improvement-drafted", None, "info",
                     "Drafted network-policy-v2.md to improve low-effectiveness skill "
                     "'network-policy' (0% approval)")

    health = await store.get_loop_health()
    assert health["flagged_count"] == 2
    assert health["with_recent_improvement"] == 1
    assert health["pct_with_improvement"] == 50.0


async def test_insights_page_shows_loop_health(client, _override_store):
    store = _override_store
    for _ in range(5):
        await store.record_skill_outcome("network-policy", "app-a", "rejected", "wrong")

    resp = await client.get("/insights")
    assert resp.status_code == 200
    assert "Loop Health" in resp.text


# ── LLM Decisions audit page ─────────────────────────────────────────────


async def test_decisions_page_renders_empty_state(client):
    resp = await client.get("/decisions")
    assert resp.status_code == 200
    assert "LLM Decisions" in resp.text
    assert "No LLM decisions logged yet" in resp.text


async def test_decisions_page_shows_fix_review_and_auto_mode_decisions(client, _override_store):
    store = _override_store
    await store.record_skill_outcome("network-policy", "my-app", "approved", "Fix is correct and safe")
    await store.log_event("HardeningAgent", "decision", "other-app", "info",
                     "AUTO-APPLY: LLM classified as safe (0.95): Adds a ConfigMap")

    resp = await client.get("/decisions")
    assert resp.status_code == 200
    assert "network-policy" in resp.text
    assert "Fix is correct and safe" in resp.text
    assert "HardeningAgent" in resp.text
    assert "Adds a ConfigMap" in resp.text
    assert "auto-applied" in resp.text


async def test_decisions_page_filters_by_attribution(client, _override_store):
    store = _override_store
    await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
    await store.record_skill_outcome("containerfile", "app-a", "rejected", "wrong base image")

    resp = await client.get("/decisions?attribution=containerfile")
    assert resp.status_code == 200
    assert "wrong base image" in resp.text
    # "fine" (network-policy's reason) shouldn't appear in the filtered decision
    # log or summary — network-policy itself may still appear in the filter
    # dropdown's <option> list, which is built from the unfiltered attribution set.
    assert ">fine<" not in resp.text


async def test_decisions_page_filters_by_decision_type(client, _override_store):
    store = _override_store
    await store.record_skill_outcome("network-policy", "app-a", "approved", "fine skill decision")
    await store.log_event("auto-mode", "decision", "app-b", "info", "AUTO-APPLY: safe: fine auto decision")

    resp = await client.get("/decisions?decision_type=fix-review")
    assert resp.status_code == 200
    assert "fine skill decision" in resp.text
    assert "fine auto decision" not in resp.text


async def test_decisions_page_shows_secret_classify_decisions(client, _override_store):
    """Regression guard for the gap README/llm_decisions.py used to document:
    classify_secret decisions previously persisted nothing and never showed
    up here (see analyzers/security.py's secret_decisions_out param)."""
    from agentit.llm_decisions import build_secret_classify_events

    store = _override_store
    for ev in build_secret_classify_events(
        [{"file_path": "config.py", "secret_type": "api_key", "is_secret": True,
          "confidence": 0.9, "reason": "Looks like a real hardcoded API key", "kept": True}],
        target_app="my-app",
    ):
        await store.log_event(**ev)

    resp = await client.get("/decisions")
    assert resp.status_code == 200
    assert "secret-classify" in resp.text
    assert "security-analyzer" in resp.text
    assert "Looks like a real hardcoded API key" in resp.text
    assert "kept" in resp.text


async def test_decisions_page_filters_by_secret_classify_decision_type(client, _override_store):
    from agentit.llm_decisions import build_secret_classify_events

    store = _override_store
    await store.record_skill_outcome("network-policy", "app-a", "approved", "fine skill decision")
    for ev in build_secret_classify_events(
        [{"file_path": "app.py", "secret_type": "password", "is_secret": False,
          "confidence": 0.8, "reason": "env var lookup, false positive", "kept": False}],
        target_app="app-b",
    ):
        await store.log_event(**ev)

    resp = await client.get("/decisions?decision_type=secret-classify")
    assert resp.status_code == 200
    assert "env var lookup, false positive" in resp.text
    assert "fine skill decision" not in resp.text


async def test_decisions_page_capability_proposal_success_outcomes_are_not_danger_badges(client, _override_store):
    """Regression guard: the outcome-badge lookup previously only
    special-cased 'approved'/'auto-applied' (green) and 'gated' (yellow),
    defaulting everything else -- including capability-proposal's genuinely
    successful 'proposed' outcome and its benign 'no-signal' no-op -- to a
    red 'danger' badge, misrepresenting a successful self-improvement cycle
    as a failure on the live page."""
    store = _override_store
    await store.log_event(
        "capability-scout", "capability-run", None, "info",
        "Opened proposal PR: Track stack signatures",
        details={"evidence": "README.md:42", "pr_url": "https://github.com/org/agentit/pull/9"},
    )
    await store.log_event(
        "capability-scout", "capability-run", None, "warning",
        "No proposal this cycle — insufficient real signal.",
        details={"evidence": "", "pr_url": None, "gate_results": []},
    )

    resp = await client.get("/decisions")
    assert resp.status_code == 200
    assert 'badge badge-success">proposed' in resp.text
    assert 'badge badge-warning">no-signal' in resp.text
    assert 'badge badge-danger">proposed' not in resp.text
    assert 'badge badge-danger">no-signal' not in resp.text
