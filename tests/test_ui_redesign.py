"""Behavior tests for docs/ui-redesign-proposal.md's implementation:

1. `fix_finding()` (routes/assessments.py) is a pure generation step -- no
   direct `apply_manifests_to_cluster()` call, no dead-work dry-run.
2. The Remediation Plan table's Fix button uses the same `fixable_categories`
   source of truth as the Findings tab, and posts `category` (a finding
   category), not `dimension`.
3. Gate app attribution: `list_gates()`/`list_all_gates()` join back to the
   assessment for an `app_name`; the 7 app-owner gate types surface on
   Assessment Detail's Actions tab and Fleet's "Needs Action" badge;
   `cluster-admin-review` stays on the separate Admin Review page/badge.
4. The orphaned `/apply` and `/create-pr` routes are gone (404).
5. GitOps-registration visibility on Fleet and Assessment Detail.
6. Nav: Gates retired as a standalone concept, Admin Review takes its place.
7. Self-Improvement gets a manual "run now" trigger, matching Catalog's.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.models import DimensionScore, Finding, RemediationItem, Severity
from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


@pytest.fixture(autouse=True)
def _mock_kube():
    """`is_gitops_registered()`/fleet's Argo enrichment both call into
    `kube`; stub them so tests aren't at the mercy of whatever cluster
    KUBECONFIG happens to point to."""
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
    """Async client with every route module touched by this redesign
    patched onto one shared, real store."""
    store = await make_store()
    async_store = store
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.gates.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store


def _report_with_network_finding(**kwargs):
    return make_report(
        scores=[
            DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="No NetworkPolicy", recommendation="Add one"),
            ]),
        ],
        **kwargs,
    )


# ── 1. fix_finding() is a pure generation step ─────────────────────────


class TestFixFindingIsPureGeneration:
    async def test_fix_does_not_call_apply_manifests_to_cluster(self, ui_client):
        client, store = ui_client
        aid = await store.save(_report_with_network_finding())

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            resp = await client.post(
                f"/assessments/{aid}/fix",
                data={"category": "network", "description": "No NetworkPolicy"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "onboard-results" in resp.headers["location"]
        mock_apply.assert_not_called()

    async def test_fix_does_not_save_apply_results(self, ui_client):
        """No apply_results row should be written by a Fix click -- that
        table is reserved for a real Deliver, not a generation step."""
        client, store = ui_client
        aid = await store.save(_report_with_network_finding())

        await client.post(
            f"/assessments/{aid}/fix",
            data={"category": "network", "description": "No NetworkPolicy"},
            follow_redirects=False,
        )

        assert await store.get_apply_results(aid) is None

    async def test_fix_still_saves_remediation_and_generates_files(self, ui_client):
        """The generation half of fix_finding() is unchanged -- only the
        direct-apply side effect was removed."""
        client, store = ui_client
        aid = await store.save(_report_with_network_finding())

        resp = await client.post(
            f"/assessments/{aid}/fix",
            data={"category": "network", "description": "No NetworkPolicy"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "fix_generated=" in resp.headers["location"]
        remediations = await store.list_remediations(aid)
        assert len(remediations) > 0

    async def test_onboard_results_flash_message_says_deliver_not_apply_or_pr(self, ui_client):
        """Regression test for the stale copy docs/ui-redesign-proposal.md
        §0 found: 'Apply to Cluster' and 'Create PR' buttons no longer
        exist on this page, so the flash message must not reference them."""
        client, store = ui_client
        aid = await store.save(_report_with_network_finding())
        await store.save_onboarding(aid, [
            {"category": "security", "path": "np.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test"},
        ])

        resp = await client.get(f"/assessments/{aid}/onboard-results?fix_generated=1&agent=security")
        assert resp.status_code == 200
        assert "apply to cluster or create a PR" not in resp.text
        assert "Review below and Deliver" in resp.text


# ── 2. Remediation Plan table's Fix button uses the shared category set ──


class TestRemediationPlanFixButtonUsesCategory:
    def test_remediation_plan_item_carries_finding_category(self):
        """models.RemediationItem now threads the finding's real category
        through, not just its dimension -- runner.generate_remediation_plan()."""
        from agentit.runner import generate_remediation_plan

        scores = [DimensionScore(dimension="security", score=20, max_score=100, findings=[
            Finding(category="container", severity=Severity.critical,
                    description="No Containerfile", recommendation="Add one"),
        ])]
        plan = generate_remediation_plan(scores)
        assert len(plan) == 1
        assert plan[0].dimension == "security"
        assert plan[0].category == "container"

    def test_remediation_item_category_defaults_to_empty_for_backward_compat(self):
        """Already-serialized report_json blobs (pre-dating this field)
        must still deserialize -- category defaults to ''."""
        item = RemediationItem(
            priority=1, dimension="security", description="x",
            estimated_effort="1h", agent_responsible="agent",
        )
        assert item.category == ""

    async def test_remediation_plan_fix_button_posts_category_not_dimension(self, ui_client):
        client, store = ui_client
        report = make_report(scores=[
            DimensionScore(dimension="security", score=20, max_score=100, findings=[
                Finding(category="container", severity=Severity.critical,
                        description="No Containerfile", recommendation="Add one"),
            ]),
        ])
        report.remediation_plan = [
            RemediationItem(
                priority=1, dimension="security", description="No Containerfile",
                estimated_effort="1h", agent_responsible="Security Agent",
                category="container",
            ),
        ]
        aid = await store.save(report)

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # The Fix form for this remediation-plan row must post the finding
        # *category* ("container"), never the *dimension* ("security").
        assert 'name="category" value="container"' in resp.text
        assert 'name="category" value="security"' not in resp.text

    async def test_remediation_plan_no_fix_button_for_unregistered_category(self, ui_client):
        """A category with no FIX_REGISTRY entry (exact or substring) must
        not show a Fix button at all -- the old keyword-substring heuristic
        was independent of the registry and could drift from it."""
        client, store = ui_client
        report = make_report(scores=[
            DimensionScore(dimension="ha_dr", score=20, max_score=100, findings=[
                Finding(category="chaos_engineering", severity=Severity.high,
                        description="No chaos experiments configured", recommendation="Add one"),
            ]),
        ])
        report.remediation_plan = [
            RemediationItem(
                priority=1, dimension="ha_dr", description="No chaos experiments configured",
                estimated_effort="1h", agent_responsible="HA/DR Agent",
                category="chaos_engineering",
            ),
        ]
        aid = await store.save(report)

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert 'name="category" value="chaos_engineering"' not in resp.text


# ── 3. Gate data plumbing + audience split ──────────────────────────────


class TestGateAppAttributionAndActionsTab:
    async def test_assessment_detail_actions_tab_shows_own_pending_gates(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="actions-app"))
        await store.create_gate(aid, "auto-mode-review", "Auto-mode gated: low confidence")

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Auto-mode gated: low confidence" in resp.text
        assert "Approve &amp; Deliver" in resp.text or "Approve & Deliver" in resp.text

    async def test_assessment_detail_actions_tab_excludes_cluster_admin_review(self, ui_client):
        """cluster-admin-review is a different audience's gate -- it must
        never show up embedded in an app owner's Actions tab (it can still
        legitimately appear in the read-only, everything-that-happened
        Timeline tab -- that's a different surface with a different job)."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="cicd-app"))
        await store.create_gate(aid, "cluster-admin-review", "CI/CD manifests need elevated review")

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        actions_tab = resp.text.split('x-show="tab === \'actions\'"', 1)[1].split('x-show="tab === \'timeline\'"', 1)[0]
        assert "CI/CD manifests need elevated review" not in actions_tab
        assert "No pending actions for this app" in actions_tab

    async def test_assessment_detail_actions_tab_empty_state(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="quiet-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "No pending actions for this app" in resp.text

    async def test_assessment_detail_actions_tab_shows_gate_from_old_assessment_of_same_app(self, ui_client):
        """Orphaned-gate-attribution regression: a gate created against an
        app's OLD assessment_id must still be visible/actionable from the
        Actions tab of that SAME app's CURRENT (re-assessed) assessment --
        `gates.assessment_id` is a FK to whichever assessment existed at
        gate-creation time, not a live pointer that follows the app forward.
        """
        client, store = ui_client
        old_aid = await store.save(make_report(repo_name="reassessed-app"))
        await store.create_gate(old_aid, "auto-mode-review", "Auto-mode gated: low confidence")

        new_aid = await store.save(make_report(repo_name="reassessed-app"))
        assert new_aid != old_aid

        resp = await client.get(f"/assessments/{new_aid}")
        assert resp.status_code == 200
        assert "Auto-mode gated: low confidence" in resp.text

    async def test_fleet_needs_action_badge_counts_app_owner_gates_only(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="needs-action-app"))
        await store.create_gate(aid, "auto-mode-review", "gate 1")
        await store.create_gate(aid, "dry-run-failed", "gate 2")
        # cluster-admin-review must NOT count toward this app's Fleet badge.
        await store.create_gate(aid, "cluster-admin-review", "gate 3")

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "2 pending" in resp.text

    async def test_fleet_no_badge_when_no_pending_gates(self, ui_client):
        client, store = ui_client
        await store.save(make_report(repo_name="clean-app"))

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "pending</span>" not in resp.text

    async def test_fleet_needs_action_badge_is_a_real_link_to_the_actions_tab(self, ui_client):
        """Regression: the "N pending" badge was a plain <span>, not a
        link, despite being the most action-oriented element on the row.
        It must link to that app's Assessment Detail Actions tab -- using
        the real client_tab_nav query-param convention, not a bare
        `#actions` anchor the Alpine-driven tabs would ignore."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="linked-badge-app"))
        await store.create_gate(aid, "auto-mode-review", "needs review")

        resp = await client.get("/")
        assert resp.status_code == 200
        assert f'<a href="/assessments/{aid}?tab=actions" class="badge badge-medium"' in resp.text

    async def test_assessment_detail_tab_query_param_opens_actions_tab(self, ui_client):
        """The badge above links with ?tab=actions -- the Alpine tab state
        must actually honor it, not always default to 'overview'."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="tab-param-app"))
        await store.create_gate(aid, "auto-mode-review", "needs review")

        resp = await client.get(f"/assessments/{aid}?tab=actions")
        assert resp.status_code == 200
        assert "x-data=\"{ tab: 'actions' }\"" in resp.text

    async def test_assessment_detail_unknown_tab_query_param_falls_back_to_overview(self, ui_client):
        """An unrecognized/garbage ?tab= value must never be interpolated
        as-is into the Alpine expression -- falls back to 'overview'."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="bad-tab-param-app"))

        resp = await client.get(f"/assessments/{aid}?tab=" + "'};alert(1);//")
        assert resp.status_code == 200
        assert "x-data=\"{ tab: 'overview' }\"" in resp.text


# ── 4. Orphaned routes retired ──────────────────────────────────────────


class TestOrphanedRoutesRetired:
    async def test_apply_route_gone(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report())
        await store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        resp = await client.post(f"/assessments/{aid}/apply", data={"namespace": "ns"})
        assert resp.status_code == 404

    async def test_create_pr_route_gone(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report())
        await store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        resp = await client.post(f"/assessments/{aid}/create-pr")
        assert resp.status_code == 404

    async def test_deliver_route_still_present(self, ui_client):
        """The one surviving, unified verb -- confirms this wasn't an
        accidental blanket removal."""
        client, store = ui_client
        aid = await store.save(make_report())
        await store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.status_code != 404


# ── 5. GitOps-registration visibility ───────────────────────────────────


class TestGitOpsVisibility:
    async def test_assessment_detail_shows_direct_apply_badge_when_unregistered(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="unregistered-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Direct apply" in resp.text
        assert "Not GitOps-registered" in resp.text

    async def test_assessment_detail_shows_gitops_badge_when_registered(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="registered-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/gitops-infra")

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert ">GitOps<" in resp.text
        assert "Not GitOps-registered" not in resp.text

    async def test_fleet_shows_gitops_badge_for_registered_app(self, ui_client, _mock_kube):
        client, store = ui_client
        await store.save(make_report(repo_name="gitops-fleet-app"))
        _mock_kube["list_custom_resources"].return_value = [
            {
                "metadata": {"name": "managed-gitops-fleet-app"},
                "spec": {"destination": {"server": "https://cluster", "namespace": "gitops-fleet-app"}},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            },
        ]
        # Fleet's Argo enrichment caches for 60s at module scope -- force a
        # fresh fetch so this test's mock is actually consulted.
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/")

        assert resp.status_code == 200
        assert ">GitOps<" in resp.text

    async def test_fleet_shows_direct_apply_badge_for_unregistered_app(self, ui_client):
        client, store = ui_client
        await store.save(make_report(repo_name="plain-fleet-app"))
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/")

        assert resp.status_code == 200
        assert ">Direct apply<" in resp.text

    async def test_register_gitops_route_sets_infra_repo_url_and_ensures_applicationset(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="to-register-app"))

        with patch("agentit.portal.github_pr.ensure_applicationset", return_value=True) as mock_ensure:
            resp = await client.post(
                f"/assessments/{aid}/register-gitops",
                data={"infra_repo_url": "https://github.com/org/gitops-infra"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]
        # Bug: this used to claim "Registered for GitOps delivery via ..." as
        # if the app were fully GitOps-registered at that instant. It isn't
        # -- is_gitops_registered() requires a live Argo CD Application,
        # which only appears after a delivery commits manifests under
        # apps/{app}/ in the infra repo and that PR is merged. The message
        # must not overclaim completion.
        from urllib.parse import unquote
        location = unquote(resp.headers["location"])
        assert "Registered for GitOps delivery via" not in location
        assert "next Fix/Onboard delivery" in location
        mock_ensure.assert_called_once_with("https://github.com/org/gitops-infra")
        report = await store.get(aid)
        assert report.infra_repo_url == "https://github.com/org/gitops-infra"

    async def test_register_gitops_then_detail_page_shows_pending_not_stale_nudge(self, ui_client, _mock_kube):
        """Root-cause regression for "Register for GitOps does nothing":
        setting infra_repo_url (what register-gitops does) is not enough to
        flip is_gitops_registered() True -- that only happens once a real
        Argo CD Application exists, which requires a delivery to actually
        land in the infra repo. Before this fix, the assessment detail page
        rendered the exact same "Not GitOps-registered" nudge with the exact
        same "Register for GitOps" button both before AND after a successful
        registration, with zero visible change -- indistinguishable from the
        button doing nothing at all."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="pending-gitops-app"))

        # Before registering: the real "not registered at all" nudge.
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Not GitOps-registered" in resp.text
        assert "Register for GitOps</button>" in resp.text

        with patch("agentit.portal.github_pr.ensure_applicationset", return_value=True):
            reg_resp = await client.post(
                f"/assessments/{aid}/register-gitops",
                data={"infra_repo_url": "https://github.com/org/gitops-infra"},
                follow_redirects=False,
            )
        assert reg_resp.status_code == 303

        # No live Application exists yet for this app (kube call succeeds,
        # simply finds none) -- the honest post-registration state.
        _mock_kube["delivery"].get_custom_resource.side_effect = None
        _mock_kube["delivery"].get_custom_resource.return_value = None

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # The stale, unchanged "not registered at all" nudge must be gone --
        # this is the visible signal that the click had an effect.
        assert "Not GitOps-registered" not in resp.text
        assert "Register for GitOps</button>" not in resp.text
        # And the page explains the real, current state instead of silently
        # looking identical or falsely claiming full registration.
        assert "GitOps infra repo configured" in resp.text
        assert "next Fix/Onboard delivery" in resp.text
        assert ">GitOps<" not in resp.text  # not fully registered yet either


# ── 6. Nav update ────────────────────────────────────────────────────────


class TestNavUpdate:
    async def test_nav_has_no_standalone_gates_link(self, ui_client):
        client, _store = ui_client
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'href="/gates"' not in resp.text

    async def test_nav_has_admin_review_link(self, ui_client):
        client, _store = ui_client
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'href="/admin-review"' in resp.text

    async def test_nav_admin_review_badge_reflects_cluster_admin_review_count_only(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="admin-badge-app"))
        await store.create_gate(aid, "cluster-admin-review", "needs elevated review")
        await store.create_gate(aid, "auto-mode-review", "app-owner gate, must not count here")

        # The nav badge counts are cached briefly at module scope (helpers.py)
        # -- force a fresh computation so this test's own gates are what
        # get counted, not whatever an earlier test in the same run left behind.
        with patch("agentit.portal.helpers._nav_gate_badges_cache",
                   {"pending_actions": 0, "admin_review": 0, "ts": 0.0}):
            resp = await client.get("/")
        assert resp.status_code == 200
        # One cluster-admin-review gate -> nav badge shows "1" next to Admin
        # Review, not 2 (the app-owner "auto-mode-review" gate must not
        # count toward this badge).
        assert 'Admin Review\n      <span class="nav-badge">1</span>' in resp.text


# ── 7. Self-Improvement "run now" trigger ───────────────────────────────


class TestSelfImprovementRunButton:
    async def test_self_improvement_page_has_run_button(self, ui_client):
        client, _store = ui_client
        resp = await client.get("/capabilities/self-improvement")
        assert resp.status_code == 200
        assert '/capabilities/self-improvement/run' in resp.text
        assert "Run Self-Improvement Scan" in resp.text

    async def test_run_route_reports_no_llm_when_unavailable(self, ui_client):
        client, _store = ui_client
        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
            resp = await client.post("/capabilities/self-improvement/run", follow_redirects=False)

        assert resp.status_code == 303
        assert "self-improvement" in resp.headers["location"]
        assert "error=" in resp.headers["location"]

    async def test_run_route_calls_research_once(self, ui_client):
        client, _store = ui_client
        with patch("agentit.watchers.capability_scout.CapabilityScout.research_once",
                   return_value={"outcome": "no-signal"}) as mock_research:
            resp = await client.post("/capabilities/self-improvement/run", follow_redirects=False)

        assert resp.status_code == 303
        mock_research.assert_called_once()
        assert "warning=" in resp.headers["location"]
