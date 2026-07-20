"""Behavior tests for docs/ui-redesign-proposal.md's implementation:

1. `fix_finding()` (routes/assessments.py) is a pure generation step -- no
   direct `apply_manifests_to_cluster()` call, no dead-work dry-run.
2. The Remediation Plan table's Fix button uses the same `fixable_categories`
   source of truth as the Findings tab, and posts `category` (a finding
   category), not `dimension`.
3. Recommendation/PR attribution: the `gates` table/generic gate-resolution
   machinery has been removed entirely (2026-07-19) -- a delivered PR
   surfaces on Assessment Detail's own Ledger tab (formerly Actions --
   merged with Timeline/PR History) via the real PR history, while the two
   remaining non-PR recommendation kinds (rollback-review,
   finding-unresolved-escalation) render via `recommendation_card` instead
   of the retired `gate_card`.
4. The orphaned `/apply` and `/create-pr` routes are gone (404).
5. GitOps-registration visibility on Fleet and Assessment Detail.
6. Nav: Gates retired as a standalone concept (Admin Review briefly took its
   place, then was itself retired 2026-07-18 -- see `TestNavUpdate`).
7. Self-Improvement gets a manual "run now" trigger, matching Catalog's.
"""
from __future__ import annotations

import re
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
        """cluster_apply.apply_manifests_to_cluster() no longer exists at
        all (deleted 2026-07-20, zero production callers) -- this is now a
        structural guarantee rather than something to mock-assert."""
        client, store = ui_client
        aid = await store.save(_report_with_network_finding())

        resp = await client.post(
            f"/assessments/{aid}/fix",
            data={"category": "network", "description": "No NetworkPolicy"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "onboard-results" in resp.headers["location"]

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

    async def test_fix_generates_files(self, ui_client):
        """The generation half of fix_finding() is unchanged -- only the
        direct-apply side effect was removed. (A prior version of this test
        also asserted a `remediations` row was saved -- that table has
        since been removed as a standalone concept entirely; the
        `fix_generated=` redirect param below is itself the real,
        already-asserted-elsewhere signal that generation succeeded.)"""
        client, store = ui_client
        aid = await store.save(_report_with_network_finding())

        resp = await client.post(
            f"/assessments/{aid}/fix",
            data={"category": "network", "description": "No NetworkPolicy"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "fix_generated=" in resp.headers["location"]

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
        # Direct Apply has been removed as a concept entirely -- the flash
        # always names "Commit & Open PR" now, never "Apply to Cluster".
        assert "Review below, then Commit &amp; Open PR" in resp.text or "Review below, then Commit & Open PR" in resp.text


# ── 1b. Per-finding Fix is post-onboard only ─────────────────────────────


class TestFixHiddenDuringOnboarding:
    """Pre-onboard Assessment Detail must not ship per-finding Fix — Scan
    (which always chains into onboarding, 2026-07-20) is the generation
    path. After onboarding, Fix returns with shared confirm + busy
    indicator (EDL)."""

    async def test_findings_fix_hidden_when_assessed(self, ui_client):
        client, store = ui_client
        aid = await store.save(_report_with_network_finding(repo_name="pre-onboard-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Scan" in resp.text
        assert f'action="/assessments/{aid}/fix"' not in resp.text
        assert "via <strong>Scan</strong> below" in resp.text
        assert "btn-label\">Fix</span>" not in resp.text

    async def test_findings_fix_shown_after_onboard_with_confirm(self, ui_client):
        client, store = ui_client
        aid = await store.save(_report_with_network_finding(repo_name="post-onboard-app"))
        await store.save_onboarding(aid, [
            {"category": "security", "path": "np.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test"},
        ])

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert f'action="/assessments/{aid}/fix"' in resp.text
        assert "Generate Fix" in resp.text
        assert "htmx-indicator" in resp.text
        assert "via <strong>Scan</strong> below" not in resp.text


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
        # Fix is hidden pre-onboard; seed onboarding so the button renders.
        await store.save_onboarding(aid, [
            {"category": "security", "path": "cm.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test"},
        ])

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # The Fix form for this remediation-plan row must post the finding
        # *category* ("container"), never the *dimension* ("security").
        assert 'name="category" value="container"' in resp.text
        assert 'name="category" value="security"' not in resp.text
        # EDL: Remediation Plan Fix must use shared confirm, never bare submit.
        assert "show-confirm" in resp.text
        assert 'Generate Fix' in resp.text

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
        await store.save_onboarding(aid, [
            {"category": "ha_dr", "path": "cm.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
             "description": "test"},
        ])

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert 'name="category" value="chaos_engineering"' not in resp.text


# ── 3. Gate data plumbing + audience split ──────────────────────────────


class TestGateAppAttributionAndActionsTab:
    """2026-07-19: Assessment Detail's Actions/Timeline/PR History tabs
    merged into one Ledger tab (docs/ledger-design-spec.md Phase 2's own
    plan for the first two, extended to fold in PR History too). The
    `gates` table/generic gate-resolution machinery has been removed
    entirely -- a PR-backed delivery (`cluster_config`/
    `cicd_shared_namespace`) is covered by the real PR list, and the two
    remaining non-PR recommendation kinds (rollback-review,
    finding-unresolved-escalation) render via `recommendation_card`
    instead of the retired `gate_card`."""

    async def test_assessment_detail_ledger_tab_shows_non_pr_pending_recommendation(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="actions-app"))
        report = await store.get(aid)
        await store.log_event(
            "slo-tracker", "rollback-recommended", report.repo_name, "warning",
            "Rollback recommended: low confidence",
        )

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Rollback recommended: low confidence" in resp.text
        assert "Roll Back" in resp.text

    async def test_assessment_detail_ledger_tab_hides_pr_backed_delivery_from_recommendation_cards(self, ui_client):
        """A delivered PR already has a real PR row -- it must not also get
        a `recommendation_card` on Assessment Detail's Ledger tab (that was
        the exact "another Approve and Deliver" duplication reported
        against this page); it shows up in the PR history table below
        instead."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="pr-gate-app"))
        report = await store.get(aid)
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/org/agentit-gitops/pull/42"}}},
        )

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": "https://github.com/org/agentit-gitops/pull/42", "title": "fix", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}?tab=ledger")
        assert resp.status_code == 200
        ledger_tab = resp.text.split('x-show="tab === \'ledger\'"', 1)[1]
        assert "Needs your review" not in ledger_tab
        assert "Waiting for your approval" in ledger_tab

    async def test_assessment_detail_ledger_tab_empty_state(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="quiet-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "No pending actions" in resp.text

    async def test_assessment_detail_ledger_tab_shows_recommendation_across_reassessment(self, ui_client):
        """An app-scoped recommendation event (rollback/escalation) is keyed
        by `target_app`, not `assessment_id` -- it must still be visible/
        actionable from the Ledger tab of that SAME app's CURRENT
        (re-assessed) assessment, not just the one that existed when it
        fired."""
        client, store = ui_client
        old_aid = await store.save(make_report(repo_name="reassessed-app"))
        old_report = await store.get(old_aid)
        await store.log_event(
            "slo-tracker", "rollback-recommended", old_report.repo_name, "warning",
            "Rollback recommended: low confidence",
        )

        new_aid = await store.save(make_report(repo_name="reassessed-app"))
        assert new_aid != old_aid

        resp = await client.get(f"/assessments/{new_aid}")
        assert resp.status_code == 200
        assert "Rollback recommended: low confidence" in resp.text

    async def test_assessment_detail_ledger_tab_shows_pr_history(self, ui_client):
        """Former PR History tab's job, folded into the Ledger tab --
        every PR AgentIT has opened for this app, newest first."""
        client, store = ui_client
        report = make_report(repo_name="pr-history-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [
            {"category": "security", "path": "x.yaml", "content": "kind: ConfigMap", "description": "x"},
        ])
        pr_url = "https://github.com/org/pr-history-app/pull/7"
        await store.update_pr_url(aid, pr_url)

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "onboard: fresh manifests", "merged_at": ""},
        ):
            resp = await client.get(f"/assessments/{aid}?tab=ledger")

        assert resp.status_code == 200
        ledger_tab = resp.text.split('x-show="tab === \'ledger\'"', 1)[1]
        assert "PR history" in ledger_tab
        assert "pull/7" in ledger_tab

    async def test_assessment_detail_tab_query_param_opens_ledger_tab(self, ui_client):
        client, store = ui_client
        aid = await store.save(make_report(repo_name="ledger-tab-param-app"))

        resp = await client.get(f"/assessments/{aid}?tab=ledger")
        assert resp.status_code == 200
        assert "x-data=\"{ tab: 'ledger', findingsCount: 0 }\"" in resp.text

    async def test_fleet_quiet_ledger_pointer_counts_prs_only(self, ui_client):
        """Fleet's quiet Ledger pointer is PR-approval-specific (Ledger's own
        job narrowed to strictly PRs -- see routes/insights.py::
        ledger_page()): non-PR recommendation events (rollback/escalation)
        must never inflate it -- only a genuinely open, unmerged PR does.
        They still show via this app's own real "pending action" row
        badge instead, not silently dropped."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="needs-action-app"))
        report = await store.get(aid)
        # Two rollback recommendations, not one rollback + one escalation --
        # an unresolved escalation takes over the per-row badge entirely
        # (see get_next_action_state()'s NEXT_ACTION_ESCALATED priority),
        # which would pre-empt the "N pending action" count below.
        await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "recommendation 1")
        await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "recommendation 2")

        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "need your approval → Ledger" not in resp.text
        assert "Needs Action" not in resp.text
        # Both non-PR recommendations still show as this app's own real
        # "pending action" row badge -- not silently dropped.
        assert "2 pending action" in resp.text

    async def test_fleet_no_pending_pointer_when_nothing_pending(self, ui_client):
        client, store = ui_client
        await store.save(make_report(repo_name="clean-app"))

        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "need your approval → Ledger" not in resp.text
        assert "pending action" not in resp.text

    async def test_fleet_pending_pointer_links_to_ledger_tab_for_non_pr_recommendations(self, ui_client):
        """A non-PR pending recommendation (rollback) never moves the
        fleet-wide "→ Ledger" pointer -- it shows via this app's own
        per-row "pending action" badge (linking straight to its own Ledger
        tab) instead, since it's not a PR the fleet-wide Ledger would ever
        list."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="linked-badge-app"))
        report = await store.get(aid)
        await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "needs review")

        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "need your approval → Ledger" not in resp.text
        assert "1 pending action" in resp.text
        assert f'/assessments/{aid}?tab=ledger' in resp.text

    async def test_assessment_detail_unknown_tab_query_param_falls_back_to_overview(self, ui_client):
        """An unrecognized/garbage ?tab= value must never be interpolated
        as-is into the Alpine expression -- falls back to 'overview'. This
        includes the now-retired 'actions'/'timeline'/'prs' tab keys --
        their content lives on the Ledger tab now."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="bad-tab-param-app"))

        resp = await client.get(f"/assessments/{aid}?tab=" + "'};alert(1);//")
        assert resp.status_code == 200
        assert "x-data=\"{ tab: 'overview', findingsCount: 0 }\"" in resp.text

        resp = await client.get(f"/assessments/{aid}?tab=actions")
        assert resp.status_code == 200
        assert "x-data=\"{ tab: 'overview', findingsCount: 0 }\"" in resp.text


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
    async def test_assessment_detail_shows_not_registered_badge_when_unregistered(self, ui_client):
        """Direct Apply has been removed as a concept entirely -- an app
        with no known infra repo shows a "Not GitOps-registered" badge, not
        a "Direct apply" badge implying a live mutating fallback exists."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="unregistered-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Direct apply" not in resp.text
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
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert ">GitOps<" in resp.text

    async def test_fleet_shows_gitops_badge_for_self_managed_app_with_matching_source(self, ui_client, _mock_kube):
        """Apps that register themselves into their own fleet (e.g. AgentIT
        via `register-self-in-fleet`) are deliberately excluded from the
        shared apps/*-directory ApplicationSet (github_pr.
        ensure_applicationset() excludes apps/agentit specifically, to avoid
        a circular/duplicate Application) and instead run under a
        hand-crafted Application named for the app itself, not
        `managed-{app}`. Regression coverage for the bug where this showed
        a permanently-stuck "GitOps (pending)" badge -- as if awaiting a
        first delivery that, by design, can never happen via that path --
        even though the app is genuinely GitOps-managed."""
        client, store = ui_client
        report = make_report(repo_name="self-managed-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/gitops-infra")
        _mock_kube["list_custom_resources"].return_value = [
            {
                "metadata": {"name": "self-managed-app"},
                "spec": {
                    "source": {"repoURL": report.repo_url + ".git"},
                    "destination": {"server": "https://cluster", "namespace": "self-managed-app"},
                },
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            },
        ]
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert ">GitOps<" in resp.text
        assert "GitOps (pending)" not in resp.text

    async def test_fleet_shows_pending_badge_when_same_named_app_source_does_not_match(self, ui_client, _mock_kube):
        """A live Application that merely happens to share an app's name
        (e.g. an unrelated, hand-created demo Application pointed at a
        placeholder repo) must NOT be mistaken for that app's own
        self-managed GitOps deployment -- only a source-repo match counts."""
        client, store = ui_client
        report = make_report(repo_name="lookalike-app")
        aid = await store.save(report)
        await store.set_infra_repo_url(aid, "https://github.com/org/gitops-infra")
        _mock_kube["list_custom_resources"].return_value = [
            {
                "metadata": {"name": "lookalike-app"},
                "spec": {
                    "source": {"repoURL": "https://github.com/someone-else/lookalike-app.git"},
                    "destination": {"server": "https://cluster", "namespace": "lookalike-app"},
                },
                "status": {"sync": {"status": "Unknown"}, "health": {"status": "Healthy"}},
            },
        ]
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert "GitOps (pending)" in resp.text

    async def test_fleet_shows_not_registered_badge_for_unregistered_app(self, ui_client):
        """Direct Apply has been removed as a concept entirely -- Fleet
        shows "Not GitOps-registered" for an app with no known infra repo,
        never a "Direct apply" badge implying a live mutating fallback."""
        client, store = ui_client
        await store.save(make_report(repo_name="plain-fleet-app"))
        with patch("agentit.portal.routes.fleet._argo_cache", {"data": {}, "ts": 0}):
            resp = await client.get("/fleet")

        assert resp.status_code == 200
        assert ">Direct apply<" not in resp.text
        assert "Not GitOps-registered" in resp.text

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
        assert 'data-action="register-gitops"' in resp.text
        assert "Register for GitOps" in resp.text

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
        assert 'data-action="register-gitops"' not in resp.text
        # And the page explains the real, current state instead of silently
        # looking identical or falsely claiming full registration.
        assert "GitOps infra repo configured" in resp.text
        assert "next Fix/Onboard delivery" in resp.text
        assert ">GitOps<" not in resp.text  # not fully registered yet either


    async def test_register_gitops_button_is_wired_not_dead_click(self, ui_client):
        """Catches the 'click does nothing' class of bugs: button must live
        inside x-data, dispatch show-confirm with a form ref, and POST to the
        real register-gitops route (not a stub / missing handler)."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="wire-gitops-app"))

        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert any(
            f"/assessments/{aid}/register-gitops" in line
            for line in resp.text.splitlines()
            if 'action="' in line
        )
        assert 'name="infra_repo_url"' in resp.text
        assert "show-confirm" in resp.text
        assert "$refs.registerGitopsForm" in resp.text
        assert 'x-data="{ dismissed: false, busy: false }"' in resp.text
        assert 'data-action="register-gitops"' in resp.text
        assert "htmx-indicator" in resp.text

    async def test_register_gitops_auto_create_failure_surfaces_error(self, ui_client):
        """Failed auto-create must redirect with a visible error — silent
        failure is exactly what made Register look like a dead control."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="fail-gitops-app"))

        with patch(
            "agentit.portal.routes.assessments._auto_create_infra_repo",
            return_value=None,
        ):
            resp = await client.post(
                f"/assessments/{aid}/register-gitops",
                data={},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        from urllib.parse import unquote
        location = unquote(resp.headers["location"])
        assert "error=" in location
        assert "infra repo" in location.lower()

        err_resp = await client.get(resp.headers["location"])
        assert err_resp.status_code == 200
        assert 'class="alert alert-error' in err_resp.text
        assert "Could not auto-create" in err_resp.text
        assert 'data-action="register-gitops"' in err_resp.text



# ── 6. Nav update ────────────────────────────────────────────────────────


class TestNavUpdate:
    async def test_nav_has_no_standalone_gates_link(self, ui_client):
        client, _store = ui_client
        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert 'href="/gates"' not in resp.text

    async def test_nav_has_no_admin_review_link(self, ui_client):
        """Admin Review (the separate, elevated-approvals nav item) was
        retired 2026-07-18 along with the `cluster-admin-review` gate type
        it existed solely for -- every gate type (and the `gates` table
        itself) is gone now, so there's no cross-app queue left to link
        here at all."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="admin-badge-app"))
        report = await store.get(aid)
        await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "needs elevated review")

        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert 'href="/admin-review"' not in resp.text
        assert "Admin Review" not in resp.text

    async def test_ledger_nav_badge_ignores_non_pr_recommendations(self, ui_client):
        """The single remaining nav badge (Ledger's) is PR-approval-specific
        (Ledger's own job narrowed to strictly PRs -- see
        routes/insights.py::ledger_page()) -- a non-PR recommendation event
        is never a PR, so it never moves it."""
        client, store = ui_client
        aid = await store.save(make_report(repo_name="admin-badge-app-2"))
        report = await store.get(aid)
        # Two rollback recommendations, not one rollback + one escalation --
        # an unresolved escalation takes over the per-row badge entirely
        # (see get_next_action_state()'s NEXT_ACTION_ESCALATED priority),
        # which would pre-empt the "N pending action" count below.
        await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "recommendation 1")
        await store.log_event("slo-tracker", "rollback-recommended", report.repo_name, "warning", "recommendation 2")

        with patch("agentit.portal.helpers._nav_gate_badges_cache", {"pending_actions": 0, "ts": 0.0}):
            resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert not re.search(r'Ledger\s*<span class="nav-badge">', resp.text)
        # Both recommendations are still real pending actions -- visible via
        # this app's own row badge instead.
        assert "2 pending action" in resp.text


# ── 7. Self-Improvement "run now" trigger ───────────────────────────────


class TestSelfImprovementRunButton:
    async def test_self_improvement_page_has_run_button(self, ui_client):
        client, _store = ui_client
        resp = await client.get("/capabilities/self-improvement")
        assert resp.status_code == 200
        assert '/capabilities/self-improvement/run' in resp.text
        assert "Run Scan" in resp.text

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
