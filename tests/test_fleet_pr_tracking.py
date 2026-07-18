"""Fleet redesign: "Open PRs"/"Total PRs" columns, the detail page's Open
PRs section + PR History tab, and the Criticality tooltip.

Real DB-backed data only (per this session's convention) -- GitHub's own
live merge/close state is the one thing that must come from a real network
call in production, so tests mock `github_pr.get_pr_status` at its
definition module (the same convention `test_portal.py`'s self-improvement-
run-detail test and `test_capability_scout.py` already use) rather than
faking a DB column that doesn't exist.
"""
from __future__ import annotations

from unittest.mock import patch

from conftest import make_report
from agentit.portal.pr_tracking import collect_pr_records, gate_pr_records, delivery_pr_records, onboarding_pr_records


# ── Unit tests: pure aggregation/normalization logic, no store/network ────


class TestGatePrRecords:
    def test_pending_gate_is_open(self):
        gates = [{"gate_type": "gitops-pr-pending", "status": "pending", "pr_url": "https://github.com/org/x/pull/1", "id": "g1"}]
        records = gate_pr_records(gates)
        assert records[0]["known_state"] == "open"

    def test_approved_gate_is_merged(self):
        gates = [{"gate_type": "gitops-pr-pending", "status": "approved", "pr_url": "https://github.com/org/x/pull/1", "id": "g1"}]
        records = gate_pr_records(gates)
        assert records[0]["known_state"] == "merged"

    def test_rejected_gate_is_closed(self):
        gates = [{"gate_type": "gitops-pr-pending", "status": "rejected", "pr_url": "https://github.com/org/x/pull/1", "id": "g1"}]
        records = gate_pr_records(gates)
        assert records[0]["known_state"] == "closed"

    def test_other_gate_types_and_missing_pr_url_are_ignored(self):
        gates = [
            {"gate_type": "auto-mode-review", "status": "pending", "pr_url": None, "id": "g1"},
            {"gate_type": "gitops-pr-pending", "status": "pending", "pr_url": None, "id": "g2"},
        ]
        assert gate_pr_records(gates) == []


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

    def test_cluster_config_outcome_is_excluded(self):
        """cluster_config's PR is already tracked, with a reliable known
        state, via its gitops-pr-pending gate -- delivery_pr_records() must
        never re-surface it a second time as an unknown-state record."""
        deliveries = [{
            "id": "d1", "mechanism": "cluster_config:infra-repo-commit",
            "details": {"outcomes": {"cluster_config": {"pr_url": "https://github.com/org/gitops/pull/3"}}},
        }]
        assert delivery_pr_records(deliveries) == []

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
        gates = [{"gate_type": "gitops-pr-pending", "status": "approved", "pr_url": "https://github.com/org/x/pull/1",
                   "id": "g1", "created_at": "2026-01-02T00:00:00"}]
        onboardings = [{"assessment_id": "a1", "pr_url": "https://github.com/org/x/pull/1", "created_at": "2026-01-01T00:00:00"}]
        records = collect_pr_records(gates, [], onboardings)
        assert len(records) == 1
        # The gate's reliable known_state wins over the duplicate onboarding record.
        assert records[0]["known_state"] == "merged"

    def test_sorted_newest_first(self):
        gates = [{"gate_type": "gitops-pr-pending", "status": "pending", "pr_url": "https://github.com/org/x/pull/1",
                   "id": "g1", "created_at": "2026-01-01T00:00:00"}]
        onboardings = [{"assessment_id": "a1", "pr_url": "https://github.com/org/x/pull/9", "created_at": "2026-02-01T00:00:00"}]
        records = collect_pr_records(gates, [], onboardings)
        assert [r["pr_url"] for r in records] == ["https://github.com/org/x/pull/9", "https://github.com/org/x/pull/1"]


# ── Integration: Fleet list columns ────────────────────────────────────────


class TestFleetPrColumns:
    async def test_fleet_shows_open_over_total_from_gitops_gate(self, portal_client):
        """A pending gitops-pr-pending gate needs no live GitHub call --
        its own status is the known, reliable "still open" fact."""
        client, store, aid = portal_client
        report = await store.get(aid)
        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/gitops/pull/11",
        )
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
    async def test_open_pr_section_lists_pending_gitops_pr(self, portal_client):
        client, store, aid = portal_client
        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/gitops/pull/20",
        )
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Open PRs (1)" in resp.text
        assert "https://github.com/org/gitops/pull/20" in resp.text

    async def test_no_open_prs_section_when_none_open(self, portal_client):
        client, store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "Open PRs (" not in resp.text

    async def test_pr_history_tab_shows_merged_outcome(self, portal_client):
        client, store, aid = portal_client
        pr_url = "https://github.com/org/gitops/pull/21"
        # resolve_gate()'s own merge step re-parses the PR URL out of the
        # gate's `summary` text (not the structured `pr_url` column) --
        # matches the shape route_and_deliver() actually creates these
        # gates with in production (see delivery.py's gate summary).
        await store.create_gate(
            aid, "gitops-pr-pending", f"PR opened: {pr_url}.", pr_url=pr_url,
        )
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        with patch("agentit.portal.github_pr.merge_pr", return_value={"merged": True, "sha": "abc"}):
            resp = await client.post(f"/gates/{gate_id}/resolve", data={"status": "approved"})
        assert resp.status_code in (200, 303)

        resp = await client.get(f"/assessments/{aid}?tab=prs")
        assert resp.status_code == 200
        assert "PR History (1)" in resp.text
        assert "Merged" in resp.text

    async def test_pr_history_tab_shows_rejected_reason(self, portal_client):
        client, store, aid = portal_client
        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/gitops/pull/22",
        )
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        resp = await client.post(
            f"/gates/{gate_id}/resolve", data={"status": "rejected", "reason": "manifest regressed a required probe"},
        )
        assert resp.status_code in (200, 303)

        resp = await client.get(f"/assessments/{aid}?tab=prs")
        assert resp.status_code == 200
        assert "Rejected" in resp.text
        assert "manifest regressed a required probe" in resp.text

    async def test_pr_history_empty_state(self, portal_client):
        client, store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}?tab=prs")
        assert resp.status_code == 200
        assert "No PRs opened for this app yet" in resp.text


# ── Criticality tooltip ─────────────────────────────────────────────────


class TestCriticalityTooltip:
    async def test_fleet_criticality_badge_has_explanatory_tooltip(self, portal_client):
        client, _store, _aid = portal_client
        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "never auto-approved" in resp.text

    async def test_assessment_detail_criticality_badge_has_explanatory_tooltip(self, portal_client):
        client, _store, aid = portal_client
        resp = await client.get(f"/assessments/{aid}")
        assert resp.status_code == 200
        assert "never auto-approved" in resp.text
