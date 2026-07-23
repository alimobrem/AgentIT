"""Fleet redesign: "Open PRs"/"Total PRs" columns, the detail page's Open
PRs section + PR History tab, and the Criticality tooltip.

The `gates` table/generic gate-resolution machinery has been removed
entirely (2026-07-19) -- every PR record here comes from
``deliveries.details_json.outcomes.<category>.pr_url`` or
``onboarding_results.pr_url``, with real state resolved via a live GitHub
check. Real DB-backed data only (per this session's convention) -- GitHub's
own live merge/close state is the one thing that must come from a real
network call in production, so tests mock `github_pr.get_pr_status` at its
definition module (the same convention `test_portal.py`'s self-improvement-
run-detail test and `test_capability_scout.py` already use) rather than
faking a DB column that doesn't exist.
"""
from __future__ import annotations

from unittest.mock import patch

from conftest import make_report
from agentit.portal.pr_tracking import collect_pr_records, delivery_pr_records, onboarding_pr_records


# ── Unit tests: pure aggregation/normalization logic, no store/network ────


class TestDeliveryPrRecords:
    def test_source_repo_pr_outcome_is_included(self):
        deliveries = [{
            "id": "d1", "mechanism": "source_patch:source-repo-pr",
            "details": {"outcomes": {"source_patch": {"pr_url": "https://github.com/org/x/pull/2"}}},
        }]
        records = delivery_pr_records(deliveries)
        assert len(records) == 1
        assert records[0]["pr_url"] == "https://github.com/org/x/pull/2"
        assert records[0]["repo_kind"] == "code"
        assert records[0]["known_state"] is None

    def test_cluster_config_outcome_is_included_too(self):
        """cluster_config's PR is delivered the same way as every other
        category now (a real GitOps-repo commit + PR, no gate row) -- it
        must surface here identically, just with repo_kind="gitops"."""
        deliveries = [{
            "id": "d1", "mechanism": "cluster_config:infra-repo-commit",
            "details": {"outcomes": {"cluster_config": {"pr_url": "https://github.com/org/gitops/pull/3"}}},
        }]
        records = delivery_pr_records(deliveries)
        assert len(records) == 1
        assert records[0]["pr_url"] == "https://github.com/org/gitops/pull/3"
        assert records[0]["repo_kind"] == "gitops"
        assert records[0]["known_state"] is None

    def test_outcome_without_pr_url_is_skipped(self):
        deliveries = [{
            "id": "d1", "mechanism": "manifest_at_rest:app-repo-pr",
            "details": {"outcomes": {"manifest_at_rest": {"error": "boom"}}},
        }]
        assert delivery_pr_records(deliveries) == []


class TestOnboardingPrRecords:
    def test_single_pr_url(self):
        onboardings = [{"assessment_id": "a1", "pr_url": "https://github.com/org/x/pull/4", "created_at": "2026-01-01T00:00:00"}]
        records = onboarding_pr_records(onboardings)
        assert len(records) == 1
        assert records[0]["pr_url"] == "https://github.com/org/x/pull/4"
        assert records[0]["repo_kind"] == "code"

    def test_pipe_joined_multiple_pr_urls_split_into_separate_records(self):
        """Per-Agent PRs writes several `|`-joined URLs into the same
        onboarding_results.pr_url column (routes/assessments.py::
        create_agent_prs_route) -- each must become its own PR record."""
        onboardings = [{
            "assessment_id": "a1",
            "pr_url": "https://github.com/org/x/pull/5 | https://github.com/org/x/pull/6",
            "created_at": "2026-01-01T00:00:00",
        }]
        records = onboarding_pr_records(onboardings)
        assert {r["pr_url"] for r in records} == {"https://github.com/org/x/pull/5", "https://github.com/org/x/pull/6"}

    def test_empty_pr_url_produces_no_records(self):
        assert onboarding_pr_records([{"assessment_id": "a1", "pr_url": "", "created_at": ""}]) == []


class TestCollectPrRecordsDedup:
    def test_dedups_by_pr_url_across_sources(self):
        """The same PR URL landing in both a delivery outcome and an
        onboarding record (e.g. onboarding's initial delivery, later
        re-surfaced via a delivery retry) must appear only once -- newest
        record wins (see _dedup_by_pr_url())."""
        deliveries = [{
            "id": "d1", "mechanism": "source_patch:source-repo-pr", "created_at": "2026-01-02T00:00:00",
            "details": {"outcomes": {"source_patch": {"pr_url": "https://github.com/org/x/pull/1"}}},
        }]
        onboardings = [{"assessment_id": "a1", "pr_url": "https://github.com/org/x/pull/1", "created_at": "2026-01-01T00:00:00"}]
        records = collect_pr_records(deliveries, onboardings)
        assert len(records) == 1
        assert records[0]["source"] == "delivery"

    def test_sorted_newest_first(self):
        deliveries = [{
            "id": "d1", "mechanism": "source_patch:source-repo-pr", "created_at": "2026-01-01T00:00:00",
            "details": {"outcomes": {"source_patch": {"pr_url": "https://github.com/org/x/pull/1"}}},
        }]
        onboardings = [{"assessment_id": "a1", "pr_url": "https://github.com/org/x/pull/9", "created_at": "2026-02-01T00:00:00"}]
        records = collect_pr_records(deliveries, onboardings)
        assert [r["pr_url"] for r in records] == ["https://github.com/org/x/pull/9", "https://github.com/org/x/pull/1"]


# ── Integration: Fleet list columns ────────────────────────────────────────


class TestFleetPrColumns:
    async def test_fleet_shows_open_over_total_from_gitops_pr(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered",
            details={"outcomes": {"cluster_config": {"pr_url": "https://github.com/org/gitops/pull/11"}}},
        )
        with patch("agentit.portal.github_pr.get_pr_status",
                    return_value={"state": "open", "html_url": "https://github.com/org/gitops/pull/11", "title": "fix", "merged_at": ""}):
            resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "1 / 1" in resp.text

    async def test_fleet_shows_dash_for_app_with_no_prs(self, portal_client):
        client, store, aid = portal_client
        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "Open PRs" in resp.text

    async def test_fleet_open_prs_reflects_live_check_for_delivery_pr(self, portal_client):
        """A source-repo-pr delivery outcome has no stored outcome of its
        own -- Fleet must reflect a live (here, mocked) GitHub state."""
        client, store, aid = portal_client
        report = await store.get(aid)
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": "https://github.com/org/app/pull/12"}}},
        )
        with patch("agentit.portal.github_pr.get_pr_status",
                    return_value={"state": "merged", "merged_at": "2026-01-05T00:00:00", "html_url": "https://github.com/org/app/pull/12", "title": "fix"}):
            resp = await client.get("/fleet")
        assert resp.status_code == 200
        # Merged -> not counted as open, but still counted in the total.
        assert "0 / 1" in resp.text


# ── Integration: Assessment Detail's Open PRs section + PR History tab ────


class TestAssessmentDetailPrHistory:
    async def test_open_pr_section_lists_open_gitops_pr(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/gitops/pull/20"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )
        with patch("agentit.portal.github_pr.get_pr_status",
                    return_value={"state": "open", "html_url": pr_url, "title": "fix", "merged_at": ""}):
            resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Open PRs (1)" in resp.text
        assert pr_url in resp.text

    async def test_open_prs_empty_state_when_none_open(self, portal_client):
        """Open PRs section is always visible; empty state is honest when
        the seeded app has no remediable findings (no Scan-for-PR nudge)."""
        client, store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Open PRs" in resp.text
        assert "No open PRs — nothing remediable to open one for right now." in resp.text
        assert "Open PRs (" not in resp.text

    async def test_ledger_tab_pr_history_shows_merged_outcome(self, portal_client):
        """Former PR History tab, merged into the Ledger tab 2026-07-19."""
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/gitops/pull/21"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )
        with patch("agentit.portal.github_pr.get_pr_status",
                    return_value={"state": "merged", "merged_at": "2026-01-05T00:00:00", "html_url": pr_url, "title": "fix"}):
            resp = await client.get(f"/assessments/{aid}?tab=ledger")
        assert resp.status_code == 200
        assert "PR history (1)" in resp.text
        assert "Merged" in resp.text

    async def test_ledger_tab_pr_history_shows_rejected_reason(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/gitops/pull/22"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )
        await store.record_pr_outcome(
            pr_url, report.repo_name, "rejected",
            assessment_id=aid, category="cluster_config", reject_reason="manifest regressed a required probe",
        )
        with patch("agentit.portal.github_pr.get_pr_status",
                    return_value={"state": "closed", "html_url": pr_url, "title": "fix", "merged_at": ""}):
            resp = await client.get(f"/assessments/{aid}?tab=ledger")
        assert resp.status_code == 200
        assert "Rejected" in resp.text
        assert "manifest regressed a required probe" in resp.text

    async def test_ledger_tab_pr_history_empty_state(self, portal_client):
        client, store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}?tab=ledger")
        assert resp.status_code == 200
        assert "No PRs opened for this app yet" in resp.text


# ── Criticality tooltip ─────────────────────────────────────────────────


class TestCriticalityTooltip:
    """Tooltip copy was re-verified and reworded (2026-07-18, "re-verify
    Criticality against current code") to describe only Criticality's two
    real effects (auto-deliver eligibility, default SLO strictness) -- the
    dead "extra deploy-approval gate" effect these tests used to assert on
    is gone from the copy along with the dead gate-list entry itself."""

    async def test_fleet_criticality_badge_has_explanatory_tooltip(self, portal_client):
        client, _store, _aid = portal_client
        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "can never auto-deliver" in resp.text

    async def test_assessment_detail_criticality_badge_has_explanatory_tooltip(self, portal_client):
        client, _store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        # Score-first Assessment Detail shortens the criticality tooltip (PR #161).
        assert "require human merge before delivery" in resp.text
