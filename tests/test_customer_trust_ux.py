"""Customer-trust UX: honest Deliver confirms + severity-gated Events badge."""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


class _ClickAttrCapture(HTMLParser):
    def __init__(self):
        super().__init__()
        self.clicks: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "@click" and value:
                self.clicks.append(value)


@pytest.fixture(autouse=True)
def _mock_kube():
    with patch("agentit.portal.delivery.kube") as mock_delivery_kube, \
         patch("agentit.kube.list_custom_resources", return_value=[]):
        mock_delivery_kube.get_custom_resource.side_effect = Exception("no cluster in tests")
        yield


@pytest.fixture
async def trust_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.insights.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            await prime_csrf(client)
            yield client, store


async def _seed_onboard(store, repo_name: str = "trust-app") -> str:
    aid = await store.save(make_report(repo_name=repo_name))
    await store.save_onboarding(aid, [
        {
            "category": "security",
            "path": "cm.yaml",
            "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: t\n",
            "description": "cm",
        },
    ])
    return aid


class TestHonestDeliverConfirm:
    async def test_direct_apply_confirm_names_namespace_not_irreversible(self, trust_client):
        client, store = trust_client
        aid = await _seed_onboard(store, "direct-ns-app")

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        text = resp.text
        assert "Apply to Cluster" in text
        assert "cannot be undone" not in text
        assert "modifies production resources and cannot be undone" not in text

        parser = _ClickAttrCapture()
        parser.feed(text)
        # Soft-gate: primary Apply has no @click until Dry Run; override
        # confirm still carries the honest Direct Apply consequence text.
        clicks = [
            c for c in parser.clicks
            if "Override" in c and "Apply to Cluster" in html_lib.unescape(c)
        ]
        assert clicks, "expected Override Apply to Cluster confirm @click"
        click = html_lib.unescape(clicks[0])
        assert "namespace direct-ns-app" in click
        assert "may modify cluster resources" in click
        assert "cannot be undone" not in click
        assert "does not mutate the cluster" not in click

    async def test_gitops_confirm_opens_pr_does_not_claim_cluster_mutation(self, trust_client):
        client, store = trust_client
        aid = await _seed_onboard(store, "gitops-trust-app")

        with patch(
            "agentit.portal.delivery.is_gitops_registered",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/infra-gitops"),
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        text = resp.text
        # Jinja autoescape turns "&" into "&amp;" in HTML text/attrs.
        assert "Commit &amp; Open PR" in text or "Commit & Open PR" in text
        assert "cannot be undone" not in text
        assert "modifies production" not in text

        parser = _ClickAttrCapture()
        parser.feed(text)
        # Soft-gate: check override confirm (primary unlocked only after Dry Run).
        clicks = [
            c for c in parser.clicks
            if "Override" in c and "Open PR" in html_lib.unescape(c)
        ]
        assert clicks, "expected Override Commit & Open PR confirm @click"
        click = html_lib.unescape(clicks[0]).encode("utf-8").decode("unicode_escape")
        assert "opens a pull request" in click.lower() or "open a PR" in click
        assert (
            "does not mutate the cluster" in click
            or "cluster is not mutated" in click
        )
        assert "cannot be undone" not in click
        assert "modifies production" not in click


class TestSeverityGatedEventsBadge:
    async def test_compute_badge_source_always_filters_severity(self, trust_client):
        client, _store = trust_client
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "_isBadgeSeverity" in html
        assert "_computeBadge" in html
        fn = html.split("function eventsDrawer()")[1].split("async openDrawer()")[0]
        assert "critical" in fn and "high" in fn
        assert "_isBadgeSeverity" in fn
        assert not re.search(
            r"return rows\.filter\(function\(e\) \{\s*return e\.timestamp",
            fn,
        ), "badge must not count all unread timestamps without severity filter"

    async def test_api_events_enriches_assessment_id_for_drawer_links(self, trust_client):
        client, store = trust_client
        aid = await store.save(make_report(repo_name="drawer-link-app"))
        await store.log_event(
            "agent-a", "alert", "drawer-link-app", "critical", "needs attention",
        )
        resp = await client.get("/api/events?limit=20")
        assert resp.status_code == 200
        rows = resp.json()
        match = [e for e in rows if e.get("target_app") == "drawer-link-app"]
        assert match
        assert match[0].get("assessment_id") == aid

    async def test_drawer_prefers_assessment_actions_href(self, trust_client):
        client, _store = trust_client
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "_eventHref" in resp.text
        assert "?tab=actions" in resp.text
        assert "needs_you=1" in resp.text
