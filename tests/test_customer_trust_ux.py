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
    async def test_onboard_results_never_claims_cluster_mutation_or_commit_cta(self, trust_client):
        """Onboard Results is Scan-results-only: no Commit CTA and no
        irreversible cluster-mutation copy (Direct Apply is gone)."""
        client, store = trust_client
        aid = await _seed_onboard(store, "direct-ns-app")

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        text = resp.text
        assert "Apply to Cluster" not in text
        assert "Commit & Open PR" not in text
        assert "Commit &amp; Open PR" not in text
        assert "cannot be undone" not in text
        assert "modifies production resources and cannot be undone" not in text

        parser = _ClickAttrCapture()
        parser.feed(text)
        clicks = [c for c in parser.clicks if "Confirm Commit" in html_lib.unescape(c)]
        assert not clicks, "Onboard Results must not offer a Commit confirm dialog"

    async def test_gitops_onboard_results_has_no_manual_deliver_confirm(self, trust_client):
        client, store = trust_client
        aid = await _seed_onboard(store, "gitops-trust-app")
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")

        with patch(
            "agentit.portal.delivery.is_gitops_registered",
            new_callable=AsyncMock,
            return_value=(True, "https://github.com/org/infra-gitops"),
        ):
            resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        text = resp.text
        assert "Commit & Open PR" not in text
        assert "Commit &amp; Open PR" not in text
        assert "cannot be undone" not in text
        assert "modifies production" not in text
        assert 'data-action="apply"' not in text

        parser = _ClickAttrCapture()
        parser.feed(text)
        clicks = [
            c for c in parser.clicks
            if "Confirm Commit" in html_lib.unescape(c) and "Open PR" in html_lib.unescape(c)
        ]
        assert not clicks, "manual Commit & Open PR confirm must not exist"


class TestSeverityGatedEventsBadge:
    async def test_compute_badge_source_always_filters_severity(self, trust_client):
        """eventsDrawer() moved out of an inline <script> block in base.html
        into static/js/events-drawer.js (2026-07-20) -- fetch that static
        file directly rather than grepping a rendered page's HTML for JS
        that no longer lives there."""
        client, _store = trust_client
        resp = await client.get("/static/js/events-drawer.js")
        assert resp.status_code == 200
        js = resp.text
        assert "_isBadgeSeverity" in js
        assert "_computeBadge" in js
        fn = js.split("function eventsDrawer()")[1].split("async openDrawer()")[0]
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

    async def test_drawer_prefers_assessment_ledger_href(self, trust_client):
        """See test_compute_badge_source_always_filters_severity's docstring
        above -- _eventHref lives in static/js/events-drawer.js now."""
        client, _store = trust_client
        resp = await client.get("/static/js/events-drawer.js")
        assert resp.status_code == 200
        assert "_eventHref" in resp.text
        assert "?tab=ledger" in resp.text
        assert "/ledger?app=" in resp.text
