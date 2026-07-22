"""Every onboarded app can have two distinct repos in play: its own code
repo (``report.repo_url``) and, once GitOps-registered, an infra/GitOps
repo (``report.infra_repo_url``) that Argo CD actually syncs manifests
from. These tests confirm Fleet and Assessment Detail show clearly
labeled, separate links to both repos (Code repo / GitOps repo), correctly
omitting the GitOps link when the app isn't GitOps-registered.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


@pytest.fixture(autouse=True)
def _mock_kube():
    """``is_gitops_registered()``/Fleet's Argo enrichment both call into
    ``kube``; stub them so tests aren't at the mercy of whatever cluster
    KUBECONFIG happens to point to (mirrors test_ui_redesign.py's fixture
    of the same name)."""
    with patch("agentit.portal.cluster_apply.kube") as mock_apply_kube, \
         patch("agentit.portal.delivery.kube") as mock_delivery_kube, \
         patch("agentit.kube.list_custom_resources") as mock_list:
        mock_apply_kube.namespace_exists.return_value = True
        mock_apply_kube.get_api_resources.return_value = set()
        mock_apply_kube.apply_yaml.return_value = {"applied": True, "error": None}
        mock_delivery_kube.get_custom_resource.side_effect = Exception("no cluster in tests")
        mock_list.return_value = []
        yield {"delivery": mock_delivery_kube, "list_custom_resources": mock_list}


@pytest.fixture
async def ui_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store


class TestFleetRepoLinks:
    async def test_shows_both_repo_links_when_gitops_registered(self, ui_client, _mock_kube):
        client, store = ui_client
        report = make_report(repo_name="fleet-both-repos-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/fleet-both-repos-gitops")

        _mock_kube["list_custom_resources"].return_value = [
            {
                "metadata": {"name": "managed-fleet-both-repos-app"},
                "spec": {"destination": {"server": "https://cluster", "namespace": "fleet-both-repos-app"}},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            },
        ]
        # Fleet's Argo enrichment caches for 60s at module scope -- force a
        # fresh fetch so this test's mock is actually consulted.
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert ">Code repo<" in resp.text
        assert f'href="{report.repo_url}"' in resp.text
        assert ">GitOps repo<" in resp.text
        assert 'href="https://github.com/org/fleet-both-repos-gitops"' in resp.text

    async def test_omits_gitops_repo_link_when_not_registered(self, ui_client, _mock_kube):
        client, store = ui_client
        report = make_report(repo_name="fleet-code-only-app")
        await store.save(report)

        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert ">Code repo<" in resp.text
        assert f'href="{report.repo_url}"' in resp.text
        assert ">GitOps repo<" not in resp.text

    async def test_omits_gitops_repo_link_when_registered_but_infra_url_unknown(self, ui_client, _mock_kube):
        """The rare edge case delivery.py's route_and_deliver() also guards:
        a live Argo CD Application exists (gitops_registered=True) but this
        app's infra_repo_url was never recorded -- there's no URL to link
        to, so the GitOps repo link must not render (vs. a broken/empty
        href)."""
        client, store = ui_client
        report = make_report(repo_name="fleet-registered-no-url-app")
        await store.save(report)

        _mock_kube["list_custom_resources"].return_value = [
            {
                "metadata": {"name": "managed-fleet-registered-no-url-app"},
                "spec": {"destination": {"server": "https://cluster", "namespace": "fleet-registered-no-url-app"}},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            },
        ]
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert ">GitOps<" in resp.text  # the existing badge still fires
        assert ">GitOps repo<" not in resp.text  # but no repo link with no URL


class TestAssessmentDetailRepoLinks:
    async def test_shows_both_repo_links_when_gitops_registered(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="detail-both-repos-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/detail-both-repos-gitops")

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # Score-first status strip uses short labels (Code: / GitOps:).
        assert ">Code:<" in resp.text
        assert f'href="{report.repo_url}"' in resp.text
        assert ">GitOps:<" in resp.text
        assert 'href="https://github.com/org/detail-both-repos-gitops"' in resp.text

    async def test_omits_gitops_repo_link_when_not_registered(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="detail-code-only-app")
        aid = await store.save(report)

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert ">Code:<" in resp.text
        assert f'href="{report.repo_url}"' in resp.text
        assert ">GitOps:<" not in resp.text
