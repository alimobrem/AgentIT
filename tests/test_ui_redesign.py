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
from fastapi.testclient import TestClient

from agentit.models import DimensionScore, Finding, RemediationItem, Severity
from agentit.portal.app import app
from agentit.portal.store_factory import AsyncSQLiteStore
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
def ui_client():
    """TestClient with every route module touched by this redesign patched
    onto one shared in-memory store."""
    store = make_store()
    async_store = AsyncSQLiteStore.wrap(store)
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.gates.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store):
        client = TestClient(app)
        prime_csrf(client)
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
    def test_fix_does_not_call_apply_manifests_to_cluster(self, ui_client):
        client, store = ui_client
        aid = store.save(_report_with_network_finding())

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            resp = client.post(
                f"/assessments/{aid}/fix",
                data={"category": "network", "description": "No NetworkPolicy"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "onboard-results" in resp.headers["location"]
        mock_apply.assert_not_called()

    def test_fix_does_not_save_apply_results(self, ui_client):
        """No apply_results row should be written by a Fix click -- that
        table is reserved for a real Deliver, not a generation step."""
        client, store = ui_client
        aid = store.save(_report_with_network_finding())

        client.post(
            f"/assessments/{aid}/fix",
            data={"category": "network", "description": "No NetworkPolicy"},
            follow_redirects=False,
        )

        assert store.get_apply_results(aid) is None

    def test_fix_still_saves_remediation_and_generates_files(self, ui_client):
        """The generation half of fix_finding() is unchanged -- only the
        direct-apply side effect was removed."""
        client, store = ui_client
        aid = store.save(_report_with_network_finding())

        resp = client.post(
            f"/assessments/{aid}/fix",
            data={"category": "network", "description": "No NetworkPolicy"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "fix_generated=" in resp.headers["location"]
        remediations = store.list_remediations(aid)
        assert len(remediations) > 0

    def test_onboard_results_flash_message_says_deliver_not_apply_or_pr(self, ui_client):
        """Regression test for the stale copy docs/ui-redesign-proposal.md
        §0 found: 'Apply to Cluster' and 'Create PR' buttons no longer
        exist on this page, so the flash message must not reference them."""
        client, store = ui_client
        aid = store.save(_report_with_network_finding())
        store.save_onboarding(aid, [
            {"category": "security", "path": "np.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test"},
        ])

        resp = client.get(f"/assessments/{aid}/onboard-results?fix_generated=1&agent=security")
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

    def test_remediation_plan_fix_button_posts_category_not_dimension(self, ui_client):
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
        aid = store.save(report)

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # The Fix form for this remediation-plan row must post the finding
        # *category* ("container"), never the *dimension* ("security").
        assert 'name="category" value="container"' in resp.text
        assert 'name="category" value="security"' not in resp.text

    def test_remediation_plan_no_fix_button_for_unregistered_category(self, ui_client):
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
        aid = store.save(report)

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert 'name="category" value="chaos_engineering"' not in resp.text


# ── 3. Gate data plumbing + audience split ──────────────────────────────


class TestGateAppAttributionAndActionsTab:
    def test_assessment_detail_actions_tab_shows_own_pending_gates(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report(repo_name="actions-app"))
        store.create_gate(aid, "auto-mode-review", "Auto-mode gated: low confidence")

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Auto-mode gated: low confidence" in resp.text
        assert "Approve &amp; Deliver" in resp.text or "Approve & Deliver" in resp.text

    def test_assessment_detail_actions_tab_excludes_cluster_admin_review(self, ui_client):
        """cluster-admin-review is a different audience's gate -- it must
        never show up embedded in an app owner's Actions tab (it can still
        legitimately appear in the read-only, everything-that-happened
        Timeline tab -- that's a different surface with a different job)."""
        client, store = ui_client
        aid = store.save(make_report(repo_name="cicd-app"))
        store.create_gate(aid, "cluster-admin-review", "CI/CD manifests need elevated review")

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        actions_tab = resp.text.split('x-show="tab === \'actions\'"', 1)[1].split('x-show="tab === \'timeline\'"', 1)[0]
        assert "CI/CD manifests need elevated review" not in actions_tab
        assert "No pending actions for this app" in actions_tab

    def test_assessment_detail_actions_tab_empty_state(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report(repo_name="quiet-app"))

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "No pending actions for this app" in resp.text

    def test_fleet_needs_action_badge_counts_app_owner_gates_only(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report(repo_name="needs-action-app"))
        store.create_gate(aid, "auto-mode-review", "gate 1")
        store.create_gate(aid, "dry-run-failed", "gate 2")
        # cluster-admin-review must NOT count toward this app's Fleet badge.
        store.create_gate(aid, "cluster-admin-review", "gate 3")

        resp = client.get("/")
        assert resp.status_code == 200
        assert "2 pending" in resp.text

    def test_fleet_no_badge_when_no_pending_gates(self, ui_client):
        client, store = ui_client
        store.save(make_report(repo_name="clean-app"))

        resp = client.get("/")
        assert resp.status_code == 200
        assert "pending</span>" not in resp.text


# ── 4. Orphaned routes retired ──────────────────────────────────────────


class TestOrphanedRoutesRetired:
    def test_apply_route_gone(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report())
        store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        resp = client.post(f"/assessments/{aid}/apply", data={"namespace": "ns"})
        assert resp.status_code == 404

    def test_create_pr_route_gone(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report())
        store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        resp = client.post(f"/assessments/{aid}/create-pr")
        assert resp.status_code == 404

    def test_deliver_route_still_present(self, ui_client):
        """The one surviving, unified verb -- confirms this wasn't an
        accidental blanket removal."""
        client, store = ui_client
        aid = store.save(make_report())
        store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        resp = client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.status_code != 404


# ── 5. GitOps-registration visibility ───────────────────────────────────


class TestGitOpsVisibility:
    def test_assessment_detail_shows_direct_apply_badge_when_unregistered(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report(repo_name="unregistered-app"))

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Direct apply" in resp.text
        assert "Not GitOps-registered" in resp.text

    def test_assessment_detail_shows_gitops_badge_when_registered(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="registered-app")
        aid = store.save(report)
        store.set_infra_repo_url(aid, "https://github.com/org/gitops-infra")

        resp = client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert ">GitOps<" in resp.text
        assert "Not GitOps-registered" not in resp.text

    def test_fleet_shows_gitops_badge_for_registered_app(self, ui_client, _mock_kube):
        client, store = ui_client
        store.save(make_report(repo_name="gitops-fleet-app"))
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
            resp = client.get("/")

        assert resp.status_code == 200
        assert ">GitOps<" in resp.text

    def test_fleet_shows_direct_apply_badge_for_unregistered_app(self, ui_client):
        client, store = ui_client
        store.save(make_report(repo_name="plain-fleet-app"))
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = client.get("/")

        assert resp.status_code == 200
        assert ">Direct apply<" in resp.text

    def test_register_gitops_route_sets_infra_repo_url_and_ensures_applicationset(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report(repo_name="to-register-app"))

        with patch("agentit.portal.github_pr.ensure_applicationset", return_value=True) as mock_ensure:
            resp = client.post(
                f"/assessments/{aid}/register-gitops",
                data={"infra_repo_url": "https://github.com/org/gitops-infra"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "success=" in resp.headers["location"]
        mock_ensure.assert_called_once_with("https://github.com/org/gitops-infra")
        report = store.get(aid)
        assert report.infra_repo_url == "https://github.com/org/gitops-infra"


# ── 6. Nav update ────────────────────────────────────────────────────────


class TestNavUpdate:
    def test_nav_has_no_standalone_gates_link(self, ui_client):
        client, _store = ui_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'href="/gates"' not in resp.text

    def test_nav_has_admin_review_link(self, ui_client):
        client, _store = ui_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'href="/admin-review"' in resp.text

    def test_nav_admin_review_badge_reflects_cluster_admin_review_count_only(self, ui_client):
        client, store = ui_client
        aid = store.save(make_report(repo_name="admin-badge-app"))
        store.create_gate(aid, "cluster-admin-review", "needs elevated review")
        store.create_gate(aid, "auto-mode-review", "app-owner gate, must not count here")

        # The nav badge counts are cached briefly at module scope (helpers.py)
        # -- force a fresh computation so this test's own gates are what
        # get counted, not whatever an earlier test in the same run left behind.
        with patch("agentit.portal.helpers._nav_gate_badges_cache",
                   {"pending_actions": 0, "admin_review": 0, "ts": 0.0}):
            resp = client.get("/")
        assert resp.status_code == 200
        # One cluster-admin-review gate -> nav badge shows "1" next to Admin
        # Review, not 2 (the app-owner "auto-mode-review" gate must not
        # count toward this badge).
        assert 'Admin Review\n      <span class="nav-badge">1</span>' in resp.text


# ── 7. Self-Improvement "run now" trigger ───────────────────────────────


class TestSelfImprovementRunButton:
    def test_self_improvement_page_has_run_button(self, ui_client):
        client, _store = ui_client
        resp = client.get("/capabilities/self-improvement")
        assert resp.status_code == 200
        assert '/capabilities/self-improvement/run' in resp.text
        assert "Run Self-Improvement Scan" in resp.text

    def test_run_route_reports_no_llm_when_unavailable(self, ui_client):
        client, _store = ui_client
        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
            resp = client.post("/capabilities/self-improvement/run", follow_redirects=False)

        assert resp.status_code == 303
        assert "self-improvement" in resp.headers["location"]
        assert "error=" in resp.headers["location"]

    def test_run_route_calls_research_once(self, ui_client):
        client, _store = ui_client
        with patch("agentit.watchers.capability_scout.CapabilityScout.research_once",
                   return_value={"outcome": "no-signal"}) as mock_research:
            resp = client.post("/capabilities/self-improvement/run", follow_redirects=False)

        assert resp.status_code == 303
        mock_research.assert_called_once()
        assert "warning=" in resp.headers["location"]
