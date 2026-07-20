"""Ledger's redesigned purpose (product direction, superseding the earlier
generic A-P event-union design docs/ledger-design-spec.md described): a
fleet-wide list of every PR AgentIT has opened, filterable by category/app,
showing each PR's real lifecycle -- waiting for approval / merged / rejected
(with the real reason) / closed. Builds on ``pr_tracking.py``'s existing
per-app aggregation, extended fleet-wide by ``collect_fleet_pr_records()``.

The `gates` table/generic gate-resolution machinery has been removed
entirely (2026-07-19) -- every PR record here comes from
``deliveries.details_json.outcomes.<category>.pr_url`` (seeded via
``store.create_delivery()``) or ``onboarding_results.pr_url``, with real
state resolved via a live GitHub check (mocked below at
``agentit.portal.github_pr.get_pr_status``, the same convention
``test_fleet_pr_tracking.py`` uses) -- never a stored gate-status proxy.
A rejected/edited-before-merge outcome is durably recorded in
``pr_outcomes`` the first time it's observed (see ``pr_outcomes.py``); tests
that need one seed it directly via ``store.record_pr_outcome()`` rather than
mocking the full GitHub-comment-parsing chain.
"""
from __future__ import annotations

from unittest.mock import patch

from conftest import make_report
from agentit.portal.pr_tracking import (
    LIFECYCLE_CLOSED,
    LIFECYCLE_MERGED,
    LIFECYCLE_NEEDS_APPROVAL,
    LIFECYCLE_REJECTED,
    annotate_lifecycle,
    collect_fleet_pr_records,
    count_fleet_prs_waiting_for_approval,
    fleet_prs_waiting_for_approval,
)


def _mock_pr_status(state: str, pr_url: str, title: str = "fix", merged_at: str = ""):
    return patch(
        "agentit.portal.github_pr.get_pr_status",
        return_value={"state": state, "html_url": pr_url, "title": title, "merged_at": merged_at},
    )


async def _seed_pr_delivery(store, aid, app_name, pr_url, category="source_patch"):
    """The current, only way a PR record comes to exist: a real delivery
    outcome carrying a ``pr_url`` -- no gate row involved."""
    await store.create_delivery(
        aid, app_name, {category: 1}, mechanism=f"{category}:source-repo-pr",
        status="delivered",
        details={"outcomes": {category: {"pr_url": pr_url}}},
    )


# ── Unit tests: lifecycle labeling, no store/network ───────────────────────


class TestAnnotateLifecycle:
    def test_open_pr_needs_approval(self):
        """Every open, unmerged PR needs approval now -- the `gates` table/
        generic gate-resolution machinery has been removed entirely
        (2026-07-19), so there's no narrower gate-tracked distinction left
        to draw; a real GitHub PR merge/close IS the one approval step for
        every category equally."""
        record = {"source": "delivery", "state": "open"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_NEEDS_APPROVAL
        assert record["needs_attention"] is True

    def test_merged_pr_is_merged(self):
        record = {"source": "delivery", "state": "merged"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_MERGED
        assert record["needs_attention"] is False

    def test_closed_pr_with_reject_reason_is_rejected_not_generic_closed(self):
        record = {"source": "delivery", "state": "closed", "reject_reason": "breaks the readiness probe"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_REJECTED
        assert record["needs_attention"] is False

    def test_closed_pr_with_no_reject_reason_is_generic_closed(self):
        """A closed-without-merge PR whose real reason hasn't been captured
        (yet, or ever) stays a plain "closed" -- never a fabricated
        reason."""
        record = {"source": "onboarding", "state": "closed"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == LIFECYCLE_CLOSED

    def test_unresolvable_state_is_unknown(self):
        record = {"source": "delivery", "state": "unknown"}
        annotate_lifecycle(record)
        assert record["lifecycle"] == "unknown"


class TestFleetPrsWaitingForApproval:
    """Ledger's "Waiting for your approval" bucket is purely PR-status-
    derived (``state == "open"``) -- every open PR counts equally now that
    there's no gate to narrow it to a subset of categories."""

    def test_open_pr_counts(self):
        records = [{"source": "delivery", "state": "open"}]
        assert fleet_prs_waiting_for_approval(records) == records

    def test_open_onboarding_pr_counts(self):
        records = [{"source": "onboarding", "state": "open"}]
        assert fleet_prs_waiting_for_approval(records) == records

    def test_merged_pr_does_not_count(self):
        records = [{"source": "delivery", "state": "merged"}]
        assert fleet_prs_waiting_for_approval(records) == []

    def test_closed_pr_does_not_count(self):
        records = [{"source": "delivery", "state": "closed"}]
        assert fleet_prs_waiting_for_approval(records) == []

    def test_unknown_state_pr_does_not_count(self):
        """A PR whose live GitHub state couldn't be resolved is neither
        confirmed open nor confirmed done -- it stays out of "waiting for
        approval" and gets surfaced in history with its own "Unknown"
        badge instead."""
        records = [{"source": "delivery", "state": "unknown"}]
        assert fleet_prs_waiting_for_approval(records) == []


# ── Integration: fleet-wide aggregation ─────────────────────────────────────


class TestCollectFleetPrRecords:
    async def test_aggregates_across_multiple_apps(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        other_aid = await store.save(make_report(repo_name="other-fleet-app"))
        other_report = await store.get(other_aid)

        await _seed_pr_delivery(store, aid, report.repo_name, "https://github.com/org/app-one/pull/1")
        await _seed_pr_delivery(store, other_aid, other_report.repo_name, "https://github.com/org/app-two/pull/2")

        with _mock_pr_status("open", "https://github.com/org/app-one/pull/1"):
            records = await collect_fleet_pr_records(store)
        # Both apps' records are aggregated regardless of live-check batching
        # order -- re-fetch with each URL's own mock to avoid cross-talk.
        with _mock_pr_status("open", "https://github.com/org/app-two/pull/2"):
            records += await collect_fleet_pr_records(store)
        urls = {r["pr_url"] for r in records}
        assert "https://github.com/org/app-one/pull/1" in urls
        assert "https://github.com/org/app-two/pull/2" in urls
        by_url = {r["pr_url"]: r for r in records}
        assert by_url["https://github.com/org/app-one/pull/1"]["app_name"] == report.repo_name
        assert by_url["https://github.com/org/app-two/pull/2"]["app_name"] == other_report.repo_name

    async def test_open_pr_needs_approval_in_fleet_records(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/9"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url)
        with _mock_pr_status("open", pr_url):
            records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == pr_url)
        assert record["needs_attention"] is True
        assert record["category"] == "source_patch"

    async def test_rejected_pr_carries_the_real_reject_reason(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/10"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url)
        # Seed the durable outcome directly -- sync_pr_outcomes() sees it's
        # already recorded (pr_outcomes_recorded_for()) and never re-derives
        # it, so this is equivalent to (and much simpler than) mocking the
        # full GitHub-comment-parsing chain.
        await store.record_pr_outcome(
            pr_url, report.repo_name, "rejected",
            assessment_id=aid, category="source_patch", reject_reason="breaks the readiness probe",
        )
        with _mock_pr_status("closed", pr_url):
            records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == pr_url)
        assert record["lifecycle"] == LIFECYCLE_REJECTED
        assert record["reject_reason"] == "breaks the readiness probe"

    async def test_merged_pr_reflects_in_fleet_records(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/11"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url)
        with _mock_pr_status("merged", pr_url, merged_at="2026-07-19T00:00:00Z"):
            records = await collect_fleet_pr_records(store)
        record = next(r for r in records if r["pr_url"] == pr_url)
        assert record["lifecycle"] == LIFECYCLE_MERGED
        assert record["needs_attention"] is False

    async def test_count_fleet_prs_waiting_for_approval_matches_open_records(self, portal_client):
        """The nav badge (base.html) and Fleet's quiet pointer banner both
        go through count_fleet_prs_waiting_for_approval() -- it must agree
        with the underlying records' real open/unmerged state."""
        client, store, aid = portal_client
        report = await store.get(aid)
        await _seed_pr_delivery(store, aid, report.repo_name, "https://github.com/org/app/pull/60", category="cluster_config")
        await _seed_pr_delivery(store, aid, report.repo_name, "https://github.com/org/app/pull/61")
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            side_effect=lambda url: {"state": "open", "html_url": url, "title": "fix", "merged_at": ""},
        ):
            count = await count_fleet_prs_waiting_for_approval(store)
        assert count == 2


# ── Integration: the /ledger route ──────────────────────────────────────────


class TestLedgerPage:
    async def test_needs_approval_section_lists_open_pr_with_real_actions(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/30"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url, category="cluster_config")

        with _mock_pr_status("open", pr_url):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (1)" in resp.text
        assert pr_url in resp.text
        # The real Merge/Close action card (pr_action_card) -- not a
        # read-only pointer.
        assert "Merge PR" in resp.text

    async def test_open_pr_with_no_stored_state_counts_as_waiting_for_approval(self, portal_client):
        """A source-repo-pr delivery outcome never gets an in-app gate row
        (the `gates` table/generic gate-resolution machinery has been
        removed entirely, 2026-07-19) -- its live GitHub state alone
        decides whether it's waiting for approval."""
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/70"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url)
        with _mock_pr_status("open", pr_url, title="fix the thing"):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (1)" in resp.text
        assert "PR history (0)" in resp.text
        assert pr_url in resp.text

    async def test_two_open_prs_both_count(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url_1 = "https://github.com/org/app/pull/71"
        pr_url_2 = "https://github.com/org/app/pull/72"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url_1, category="cluster_config")
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url_2)
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            side_effect=lambda url: {"state": "open", "html_url": url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (2)" in resp.text
        assert pr_url_1 in resp.text
        assert pr_url_2 in resp.text

    async def test_closed_pr_stays_in_history(self, portal_client):
        """A PR that's genuinely closed (not open) must land in the history
        table, not "Waiting for your approval"."""
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/73"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url)
        with _mock_pr_status("closed", pr_url):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (0)" in resp.text
        assert pr_url in resp.text

    async def test_history_shows_merged_pr(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/31"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url, category="cluster_config")
        with _mock_pr_status("merged", pr_url, merged_at="2026-07-19T00:00:00Z"):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Waiting for your approval (0)" in resp.text
        assert "Merged" in resp.text
        assert pr_url in resp.text

    async def test_history_shows_rejected_pr_with_real_reason(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url = "https://github.com/org/app/pull/32"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url, category="cluster_config")
        await store.record_pr_outcome(
            pr_url, report.repo_name, "rejected",
            assessment_id=aid, category="cluster_config", reject_reason="manifest regressed a required probe",
        )
        with _mock_pr_status("closed", pr_url):
            resp = await client.get("/ledger")
        assert resp.status_code == 200
        assert "Rejected" in resp.text
        assert "manifest regressed a required probe" in resp.text

    async def test_filter_by_app(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        other_aid = await store.save(make_report(repo_name="filter-other-app"))
        other_report = await store.get(other_aid)
        pr_url_x = "https://github.com/org/x/pull/40"
        pr_url_y = "https://github.com/org/y/pull/41"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url_x, category="cluster_config")
        await _seed_pr_delivery(store, other_aid, other_report.repo_name, pr_url_y, category="cluster_config")

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            side_effect=lambda url: {"state": "open", "html_url": url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get(f"/ledger?app={report.repo_name}")
        assert resp.status_code == 200
        assert pr_url_x in resp.text
        assert pr_url_y not in resp.text

    async def test_filter_by_category(self, portal_client):
        client, store, aid = portal_client
        report = await store.get(aid)
        pr_url_50 = "https://github.com/org/x/pull/50"
        pr_url_51 = "https://github.com/org/x/pull/51"
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url_50, category="cluster_config")
        await _seed_pr_delivery(store, aid, report.repo_name, pr_url_51, category="source_patch")
        with patch(
            "agentit.portal.github_pr.get_pr_status",
            side_effect=lambda url: {"state": "open", "html_url": url, "title": "fix", "merged_at": ""},
        ):
            resp = await client.get("/ledger?category=source_patch")
        assert resp.status_code == 200
        assert pr_url_51 in resp.text
        assert pr_url_50 not in resp.text

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
