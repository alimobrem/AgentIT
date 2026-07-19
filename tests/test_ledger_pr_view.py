"""Ledger's redesigned purpose (product direction, superseding the earlier
generic A-P event-union design docs/ledger-design-spec.md described): a
fleet-wide list of every PR AgentIT has opened, filterable by category/app,
showing each PR's real lifecycle -- waiting for approval / open / merged /
rejected (with the real reason) / closed. Builds on
``pr_tracking.py``'s existing per-app aggregation, extended fleet-wide by
``collect_fleet_pr_records()``.

Real DB-backed data only (this session's convention) -- GitHub's own live
merge/close state for the un-gated PR types (source-repo-pr/app-repo-pr/
onboarding) is the one thing that must come from a real network call in
production, so these tests mock ``github_pr.get_pr_status`` at its
definition module, the same convention ``test_fleet_pr_tracking.py`` uses.
"""
from __future__ import annotations

from unittest.mock import patch

from conftest import make_report
from agentit.portal.pr_tracking import (
    LIFECYCLE_CLOSED,
    LIFECYCLE_MERGED,
    LIFECYCLE_NEEDS_APPROVAL,
    LIFECYCLE_OPEN,
    LIFECYCLE_REJECTED,
    annotate_lifecycle,
    collect_fleet_pr_records,
    count_fleet_prs_waiting_for_approval,
    fleet_prs_waiting_for_approval,
)


# ── Unit tests: lifecycle labeling, no store/network ───────────────────────


class TestAnnotateLifecycle:
    def test_pending_gate_needs_approval(self):
        record = {"source": "gate", "gate_status": "pending", "state": "open"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_NEEDS_APPROVAL
        assert record["needs_attention"] is True

    def test_approved_gate_is_merged(self):
        record = {"source": "gate", "gate_status": "approved", "state": "merged"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_MERGED
        assert record["needs_attention"] is False

    def test_rejected_gate_is_rejected_not_generic_closed(self):
        record = {"source": "gate", "gate_status": "rejected", "state": "closed"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_REJECTED

    def test_open_delivery_pr_is_open_not_needs_approval(self):
        """A source-repo-pr/app-repo-pr/onboarding PR is never gated inside
        AgentIT -- review/merge happens directly on GitHub -- so it must
        never claim "needs approval" even while genuinely open."""
        record = {"source": "delivery", "gate_status": None, "state": "open"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_OPEN
        assert record["needs_attention"] is False

    def test_merged_delivery_pr_is_merged(self):
        record = {"source": "delivery", "gate_status": None, "state": "merged"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_MERGED

    def test_closed_delivery_pr_is_closed(self):
        record = {"source": "onboarding", "gate_status": None, "state": "closed"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_CLOSED

    def test_unresolvable_state_is_unknown(self):
        record = {"source": "delivery", "gate_status": None, "state": "unknown"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == "unknown"


class TestFleetPrsWaitingForApproval:
    """2026-07-19 fix: Ledger's "Waiting for your approval" bucket is purely
    PR-status-derived (``state == "open"``), not the narrower gate-tracked
    ``needs_attention``/``lifecycle == "needs_approval"`` flag -- a
    source-repo-pr/app-repo-pr/onboarding PR is never gated inside AgentIT
    at all (see pr_tracking.py's module docstring), so the old
    ``needs_attention``-only definition silently dropped every one of those
    from this bucket even while genuinely open and unreviewed on GitHub."""

    def test_pending_gate_pr_counts(self):
        records = [{"source": "gate", "gate_status": "pending", "state": "open"}]
        assert fleet_prs_waiting_for_approval(records) == records

    def test_open_delivery_pr_with_no_gate_counts(self):
        """The exact bug this fixes: a genuinely open PR with no gate row
        at all must still show up as waiting for approval."""
        records = [{"source": "delivery", "gate_status": None, "state": "open"}]
        assert fleet_prs_waiting_for_approval(records) == records

    def test_open_onboarding_pr_with_no_gate_counts(self):
        records = [{"source": "onboarding", "gate_status": None, "state": "open"}]
        assert fleet_prs_waiting_for_approval(records) == records

    def test_merged_pr_does_not_count(self):
        records = [{"source": "delivery", "gate_status": None, "state": "merged"}]
        assert fleet_prs_waiting_for_approval(records) == []

    def test_closed_pr_does_not_count(self):
        records = [{"source": "gate", "gate_status": "rejected", "state": "closed"}]
        assert fleet_prs_waiting_for_approval(records) == []

    def test_unknown_state_pr_does_not_count(self):
        """A PR whose live GitHub state couldn't be resolved is neither
        confirmed open nor confirmed done -- it stays out of both "waiting
        for approval" (not confirmed open) and gets surfaced in history
        with its own "Unknown" badge instead."""
        records = [{"source": "delivery", "gate_status": None, "state": "unknown"}]
        assert fleet_prs_waiting_for_approval(records) == []


# ── Integration: fleet-wide aggregation ─────────────────────────────────────


class TestCollectFleetPrRecords:
    async def test_aggregates_across_multiple_apps(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        other_aid = await store.save(make_report(repo_name="other-fleet-app"))
        other_report = await store.get(other_aid)

        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/app-one/pull/1",
        )
        await store.create_gate(
            other_aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/app-two/pull/2",
        )

        records = await collect_fleet_pr_records(store)
        urls = {r["pr_url"] for r in records}
        assert "https://github.com/org/app-one/pull/1" in urls
        assert "https://github.com/org/app-two/pull/2" in urls
        by_url = {r["pr_url"]: r for r in records}
        assert by_url["https://github.com/org/app-one/pull/1"]["app_name"] == report.repo_name
        assert by_url["https://github.com/org/app-two/pull/2"]["app_name"] == other_report.repo_name

    async def test_pending_gate_record_carries_the_raw_gate_for_the_approve_reject_card(self, portal_client):
        client, store, aid = portal_client
        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened: https://github.com/org/app/pull/9.",
            pr_url="https://github.com/org/app/pull/9",
        )
        records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == "https://github.com/org/app/pull/9")
        assert record["needs_attention"] is True
        assert record["raw"]["gate_type"] == "gitops-pr-pending"
        assert record["raw"]["id"]

    async def test_rejected_gate_carries_the_real_reject_reason(self, portal_client):
        client, store, aid = portal_client
        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/app/pull/10",
        )
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        resp = await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "rejected", "reason": "breaks the readiness probe"},
        )
        assert resp.status_code in (200, 303)

        records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == "https://github.com/org/app/pull/10")
        assert record["lifecycle"] == LIFECYCLE_REJECTED
        assert record["reject_reason"] == "breaks the readiness probe"

    async def test_merged_gate_reflects_in_fleet_records(self, portal_client):
        client, store, aid = portal_client
        pr_url = "https://github.com/org/app/pull/11"
        await store.create_gate(aid, "gitops-pr-pending", f"PR opened: {pr_url}.", pr_url=pr_url)
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        with patch("agentit.portal.github_pr.merge_pr", return_value={"merged": True, "sha": "abc"}):
            resp = await client.post(f"/gates/{gate_id}/resolve", data={"status": "approved"})
        assert resp.status_code in (200, 303)

        records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == pr_url)
        assert record["lifecycle"] == LIFECYCLE_MERGED
        assert record["needs_attention"] is False

    async def test_delivery_pr_with_no_gate_uses_live_github_state(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": "https://github.com/org/app/pull/12"}}},
        )
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": "https://github.com/org/app/pull/12", "title": "fix", "merged_at": ""},
        ):
            records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == "https://github.com/org/app/pull/12")
        assert record["lifecycle"] == LIFECYCLE_OPEN
        assert record["needs_attention"] is False
        assert record["category"] == "source_patch"

    async def test_count_fleet_prs_waiting_for_approval_matches_open_records(self, portal_client):
        """2026-07-19 fix: the nav badge (base.html) and Fleet's quiet
        pointer banner both go through count_fleet_prs_waiting_for_approval()
        instead of a gate-only count -- it must agree with the underlying
        records' real open/unmerged state, gate-tracked or not."""
        client, store, aid = portal_client
        report = await store.get(aid)
        await store.create_gate(
            aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/app/pull/60",
        )
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": "https://github.com/org/app/pull/61"}}},
        )
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": "https://github.com/org/app/pull/61", "title": "fix", "merged_at": ""},
        ):
            count = await count_fleet_prs_waiting_for_approval(store)
        assert count == 2


# ── Integration: the /ledger route ──────────────────────────────────────────


class TestLedgerPage:
    async def test_needs_approval_section_lists_pending_pr_gate_with_real_actions(self, portal_client):
        client, store, aid = portal_client
        pr_url = "https://github.com/org/app/pull/30"
        await store.create_gate(aid, "gitops-pr-pending", f"PR opened: {pr_url}.", pr_url=pr_url)

        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (1)" in resp.text
        assert pr_url in resp.text
        # The exact same Approve & Deliver / Reject / Dismiss actions
        # Admin Review already uses -- not a second, read-only copy.
        # (Assessment Detail's own Ledger tab deliberately does NOT
        # duplicate this gate_card for a PR-backed gate type like this one
        # anymore -- see assessment_detail.html's Ledger-tab comment.)
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        assert f'/gates/{gate_id}/resolve' in resp.text
        assert "Approve &amp; Deliver" in resp.text or "Approve & Deliver" in resp.text

    async def test_non_pr_gate_never_appears_in_needs_approval(self, portal_client):
        client, store, aid = portal_client
        await store.create_gate(aid, "auto-mode-review", "needs review")

        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (0)" in resp.text

    async def test_open_delivery_pr_with_no_gate_counts_as_waiting_for_approval(self, portal_client):
        """The exact bug this fixes: a source-repo-pr delivery outcome
        never gets an in-app gate row (see pr_tracking.py's module
        docstring) -- before 2026-07-19 it silently fell into the
        read-only "PR history" table below instead of here, even while
        genuinely open and unreviewed on GitHub."""
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/70"
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": pr_url}}},
        )
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": pr_url, "title": "fix the thing", "merged_at": ""},
        ):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (1)" in resp.text
        assert "PR history (0)" in resp.text
        assert pr_url in resp.text
        # No in-app gate exists for this PR -- it renders as a plain
        # GitHub pointer, not an Approve & Deliver/Reject action card.
        assert "Approve &amp; Deliver" not in resp.text and "Approve & Deliver" not in resp.text
        assert "No in-app approval step for this PR type" in resp.text

    async def test_mixed_gate_and_non_gate_open_prs_both_count(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        gate_pr_url = "https://github.com/org/app/pull/71"
        delivery_pr_url = "https://github.com/org/app/pull/72"
        await store.create_gate(aid, "gitops-pr-pending", "PR opened", pr_url=gate_pr_url)
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": delivery_pr_url}}},
        )
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": delivery_pr_url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (2)" in resp.text
        assert gate_pr_url in resp.text
        assert delivery_pr_url in resp.text

    async def test_closed_delivery_pr_with_no_gate_stays_in_history(self, portal_client):
        """A delivery PR that's genuinely closed (not open) must still land
        in the history table, not "Waiting for your approval" -- this fix
        only broadens what counts as *open*, it doesn't move everything."""
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/73"
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": pr_url}}},
        )
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "closed", "html_url": pr_url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (0)" in resp.text
        assert pr_url in resp.text

    async def test_history_shows_merged_pr(self, portal_client):
        client, store, aid = portal_client
        pr_url = "https://github.com/org/app/pull/31"
        await store.create_gate(aid, "gitops-pr-pending", f"PR opened: {pr_url}.", pr_url=pr_url)
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        with patch("agentit.portal.github_pr.merge_pr", return_value={"merged": True, "sha": "abc"}):
            await client.post(f"/gates/{gate_id}/resolve", data={"status": "approved"})

        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (0)" in resp.text
        assert "Merged" in resp.text
        assert pr_url in resp.text

    async def test_history_shows_rejected_pr_with_real_reason(self, portal_client):
        client, store, aid = portal_client
        pr_url = "https://github.com/org/app/pull/32"
        await store.create_gate(aid, "gitops-pr-pending", "PR opened", pr_url=pr_url)
        gates = await store.list_gates_for_assessment(aid, status="pending")
        gate_id = next(g["id"] for g in gates if g["gate_type"] == "gitops-pr-pending")
        await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "rejected", "reason": "manifest regressed a required probe"},
        )

        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Rejected" in resp.text
        assert "manifest regressed a required probe" in resp.text

    async def test_filter_by_app(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        other_aid = await store.save(make_report(repo_name="filter-other-app"))
        await store.create_gate(aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/x/pull/40")
        await store.create_gate(other_aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/y/pull/41")

        resp = await client.get(f"/ledger?app={report.repo_name}")
        assert resp.status_code == 200
        assert "https://github.com/org/x/pull/40" in resp.text
        assert "https://github.com/org/y/pull/41" not in resp.text

    async def test_filter_by_category(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        await store.create_gate(aid, "gitops-pr-pending", "PR opened", pr_url="https://github.com/org/x/pull/50")
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": 1}, mechanism="source_patch:source-repo-pr",
            status="delivered",
            details={"outcomes": {"source_patch": {"pr_url": "https://github.com/org/x/pull/51"}}},
        )
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "open", "html_url": "https://github.com/org/x/pull/51", "title": "fix", "merged_at": ""},
        ):
            resp = await client.get("/ledger?category=source_patch")
        assert resp.status_code == 200
        assert "https://github.com/org/x/pull/51" in resp.text
        assert "https://github.com/org/x/pull/50" not in resp.text

    async def test_empty_state_when_no_prs_ever(self, portal_client):
        client, _store, _aid = portal_client
        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "No PR history yet" in resp.text
        assert "Nothing waiting on you" in resp.text

    async def test_ledger_never_renders_the_old_generic_card_type_filter(self, portal_client):
        """Regression guard for the redesign: the old A-P card_type filter
        dropdown must not still be here alongside/instead of the new
        category/app/status filters."""
        client, _store, _aid = portal_client
        resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "All card types" not in resp.text
        assert 'name="category"' in resp.text
        assert 'name="app"' in resp.text
        assert 'name="lifecycle"' in resp.text
