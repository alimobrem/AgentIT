"""Unit tests for System Health card deep-links (console / GitHub / Tekton / Argo / Observe)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from agentit.portal.health_links import (
    build_health_card_links,
    console_observe_metrics_url,
    console_resource_url,
    console_url_from_route_host,
    enrich_argo_apps_with_links,
    enrich_pipelines_with_links,
    resolve_console_url,
    resolve_github_repo_url,
)
from conftest import prime_csrf

CONSOLE = "https://console-openshift-console.apps.example.com"
REPO = "https://github.com/alimobrem/AgentIT"


def test_console_url_from_route_host():
    assert console_url_from_route_host("agentit.apps.example.com") == CONSOLE
    assert console_url_from_route_host("localhost") is None
    assert console_url_from_route_host("") is None


def test_resolve_github_repo_url_normalizes():
    assert resolve_github_repo_url("https://github.com/alimobrem/AgentIT.git") == REPO
    assert resolve_github_repo_url("git@github.com:alimobrem/AgentIT.git") == REPO
    assert resolve_github_repo_url("") is None
    assert resolve_github_repo_url("not-a-url") is None


def test_resolve_console_url_prefers_env(monkeypatch):
    monkeypatch.setenv("AGENTIT_CONSOLE_URL", CONSOLE + "/")
    assert resolve_console_url() == CONSOLE


def test_resolve_console_url_ignores_invalid_env(monkeypatch):
    monkeypatch.setenv("AGENTIT_CONSOLE_URL", "javascript:alert(1)")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert resolve_console_url() is None


def test_console_resource_and_observe_urls():
    assert console_resource_url(CONSOLE, "agentit", "pods") == f"{CONSOLE}/k8s/ns/agentit/pods"
    assert console_resource_url(CONSOLE, "agentit", "tekton.dev~v1~PipelineRun", "run-1") == (
        f"{CONSOLE}/k8s/ns/agentit/tekton.dev~v1~PipelineRun/run-1"
    )
    observe = console_observe_metrics_url(CONSOLE, "agentit")
    assert observe.startswith(f"{CONSOLE}/monitoring/query-browser?")
    assert "namespace" in observe


def test_build_health_card_links_with_console_and_github():
    cards = build_health_card_links(
        console_url=CONSOLE,
        github_repo_url=REPO,
        namespace="agentit",
        latest_pipeline_name="agentit-ci-abc",
        last_successful_ci_name="agentit-ci-good",
        current_commit="abcdef0123456789",
        kafka_name="agentit-kafka",
    )
    assert cards["platform"]["href"].startswith(f"{CONSOLE}/monitoring/query-browser")
    assert cards["rollout"]["href"].endswith("/argoproj.io~v1alpha1~Rollout/agentit")
    assert cards["pods"]["href"].endswith("/pods")
    assert cards["pipeline"]["href"].endswith("/tekton.dev~v1~PipelineRun/agentit-ci-abc")
    assert cards["pipeline"]["external"] is True
    assert cards["deployed_commit"]["href"] == f"{REPO}/commit/abcdef0123456789"
    assert cards["last_successful_ci"]["href"].endswith("/tekton.dev~v1~PipelineRun/agentit-ci-good")
    assert cards["argo_app"]["href"].endswith("/argoproj.io~v1alpha1~Application/agentit")
    assert cards["github_actions"]["href"] == f"{REPO}/actions"
    assert cards["kafka"]["href"].endswith("/kafka.strimzi.io~v1beta2~Kafka/agentit-kafka")


def test_build_health_card_links_portal_fallback_without_console():
    cards = build_health_card_links(
        console_url=None,
        github_repo_url=REPO,
        namespace="agentit",
        latest_pipeline_name="agentit-ci-abc",
        last_successful_ci_name="agentit-ci-good",
        current_commit="abc123",
    )
    assert cards["platform"]["href"] is None
    assert "console" in (cards["platform"]["reason"] or "").lower()
    assert cards["pipeline"]["href"] == "/health/pipelines/agentit-ci-abc"
    assert cards["pipeline"]["external"] is False
    assert cards["last_successful_ci"]["href"] == "/health/pipelines/agentit-ci-good"
    assert cards["deployed_commit"]["href"] == f"{REPO}/commit/abc123"
    assert cards["argo_app"]["href"] is None


def test_build_health_card_links_omits_unresolved_destinations():
    cards = build_health_card_links(
        console_url=None,
        github_repo_url=None,
        namespace="agentit",
    )
    assert cards["pipeline"]["href"] is None
    assert cards["deployed_commit"]["href"] is None
    assert cards["github_repo"]["href"] is None


def test_enrich_argo_and_pipelines():
    apps = enrich_argo_apps_with_links([{"name": "agentit", "sync": "Synced", "health": "Healthy"}], CONSOLE)
    assert apps[0]["href"].endswith("~Application/agentit")
    pipes = enrich_pipelines_with_links([{"name": "run-1", "status": "Succeeded"}], CONSOLE, "agentit")
    assert pipes[0]["console_href"].endswith("~PipelineRun/run-1")
    assert pipes[0]["portal_href"] == "/health/pipelines/run-1"
    no_console = enrich_argo_apps_with_links([{"name": "agentit"}], None)
    assert no_console[0]["href"] is None


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as c:
        await prime_csrf(c)
        yield c


async def test_health_page_renders_clickable_stat_cards(client, monkeypatch):
    monkeypatch.setenv("AGENTIT_CONSOLE_URL", CONSOLE)
    fake = {
        "argo_apps": [{"name": "agentit", "sync": "Synced", "health": "Healthy",
                       "href": f"{CONSOLE}/k8s/ns/openshift-gitops/argoproj.io~v1alpha1~Application/agentit",
                       "href_title": "Open in OpenShift console"}],
        "argo_synced": True,
        "pods": [], "pods_running": 2, "pods_failed": 0,
        "pipelines": [{"name": "agentit-ci-1", "status": "Succeeded", "duration": "ok",
                       "console_href": f"{CONSOLE}/k8s/ns/agentit/tekton.dev~v1~PipelineRun/agentit-ci-1",
                       "portal_href": "/health/pipelines/agentit-ci-1"}],
        "pipeline_status": "Succeeded",
        "kafka_ready": True, "publisher_ok": True,
        "namespace": "agentit", "cluster_url": "local",
        "kafka_stats": {"available": False, "topics": {}, "consumer_groups": []},
        "current_commit": "abcdef012345",
        "last_successful_ci": "2026-07-16T10:00:00",
        "rollout_phase": "Healthy",
        "circuit_breakers": {},
        "card_links": build_health_card_links(
            console_url=CONSOLE,
            github_repo_url=REPO,
            namespace="agentit",
            latest_pipeline_name="agentit-ci-1",
            last_successful_ci_name="agentit-ci-1",
            current_commit="abcdef0123456789",
            kafka_name="agentit-kafka",
        ),
        "deploy_status": {
            "state": "idle", "running": {"version": "0.1.0", "commit": "abcdef", "image_tag": "abcdef"},
            "pipeline": None, "argo": None, "commit_info": None, "errors": [], "resolved": None, "reason": None,
        },
    }
    with patch("agentit.portal.routes.health._get_cluster_health", return_value=fake), \
            patch("agentit.portal.routes.health._get_deploy_status", return_value=fake["deploy_status"]):
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert "System Health" in resp.text
    assert 'class="stat-card"' in resp.text
    assert f'href="{CONSOLE}/monitoring/query-browser' in resp.text
    assert f'href="{CONSOLE}/k8s/ns/agentit/argoproj.io~v1alpha1~Rollout/agentit"' in resp.text
    assert f'href="{CONSOLE}/k8s/ns/agentit/pods"' in resp.text
    assert f'href="{CONSOLE}/k8s/ns/agentit/tekton.dev~v1~PipelineRun/agentit-ci-1"' in resp.text
    assert f'href="{REPO}/commit/abcdef0123456789"' in resp.text
    assert f'href="{CONSOLE}/k8s/ns/openshift-gitops/argoproj.io~v1alpha1~Application/agentit"' in resp.text
    assert "hx-boost=\"false\"" in resp.text
    assert 'target="_blank"' in resp.text
