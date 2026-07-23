"""Tests for Phase 3 of docs/onboarding-loop-vision-gap-analysis.md's
finding-scoped re-verification: does a delivery's specific target finding
actually clear on a later re-assessment?

Covers:
- `store.create_delivery(target_findings=...)`/`list_deliveries_pending_
  finding_check()` persistence and queueing.
- `delivery.correlate_delivery_finding()`'s three real states (resolved,
  still_present, pending) plus the "unknown" no-target-recorded case.
- `delivery.check_pending_delivery_verifications()` wired end-to-end
  through `webhook_github_push`'s existing diff-triggered flow: a prior
  delivery's target finding is checked automatically on the next
  push-triggered re-assessment, the outcome is persisted, a real event is
  logged, and it maps to a real Ledger card.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.assessment_diff import finding_key
from agentit.ledger import get_ledger_cards
from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.delivery import (
    MECHANISM_INFRA_REPO_COMMIT,
    check_pending_delivery_verifications,
    correlate_delivery_finding,
)
from conftest import make_async_store, make_report

# Pending finding-check queue requires an opened PR (see
# list_deliveries_pending_finding_check) — tests that expect a delivery to
# be queued must record a pr_url outcome.
_PR_OUTCOME_DETAILS = {
    "outcomes": {"cluster_config": {"pr_url": "https://github.com/example/gitops/pull/1"}},
}


def _report_with_network_finding(**kwargs):
    report = make_report(**kwargs)
    report.scores = [
        DimensionScore(
            dimension="security", score=60, max_score=100,
            findings=[Finding(category="network", severity=Severity.medium,
                               description="Missing NetworkPolicy", recommendation="Add one")],
        ),
    ]
    report.overall_score = 60
    return report


def _report_with_unrelated_finding(**kwargs):
    report = make_report(**kwargs)
    report.scores = [
        DimensionScore(
            dimension="security", score=90, max_score=100,
            findings=[Finding(category="cost", severity=Severity.low,
                               description="Unrelated cost finding", recommendation="n/a")],
        ),
    ]
    report.overall_score = 90
    return report


def _push_body(repo_url: str) -> dict:
    return {
        "ref": "refs/heads/main",
        "repository": {"html_url": repo_url, "default_branch": "main"},
        "pusher": {"name": "tester"},
        "after": "abcdef012345",
        "commits": [],
    }


class TestCorrelateDeliveryFinding:
    async def test_unknown_when_no_target_findings_recorded(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="corr-app"))
        delivery_id = await store.create_delivery(
            aid, "corr-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
        )
        delivery = await store.get_delivery(delivery_id)

        outcome = await correlate_delivery_finding(store, delivery, _report_with_network_finding(repo_name="corr-app"))
        assert outcome["status"] == "unknown"

    async def test_pending_when_no_subsequent_assessment_exists_yet(self):
        store, _ = await make_async_store()
        report = _report_with_network_finding(repo_name="corr-app")
        aid = await store.save(report)
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            aid, "corr-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
        )
        delivery = await store.get_delivery(delivery_id)

        outcome = await correlate_delivery_finding(store, delivery, None)
        assert outcome["status"] == "pending"
        assert outcome["target_findings"] == [target]

    async def test_resolved_when_target_finding_no_longer_present(self):
        store, _ = await make_async_store()
        report = _report_with_network_finding(repo_name="corr-app")
        aid = await store.save(report)
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            aid, "corr-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
        )
        delivery = await store.get_delivery(delivery_id)

        new_report = _report_with_unrelated_finding(repo_name="corr-app")
        outcome = await correlate_delivery_finding(store, delivery, new_report)

        assert outcome["status"] == "resolved"
        assert outcome["resolved_findings"] == [list(target)] or outcome["resolved_findings"] == [target]
        assert outcome["still_present_findings"] == []

    async def test_still_present_when_target_finding_unchanged(self):
        """The core case this whole phase exists for: a finding that's
        completely unchanged between the two assessments (never flagged as
        "new" or "resolved" by diff_assessments() itself) must still be
        detected as still-present."""
        store, _ = await make_async_store()
        report = _report_with_network_finding(repo_name="corr-app")
        aid = await store.save(report)
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            aid, "corr-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
        )
        delivery = await store.get_delivery(delivery_id)

        new_report = _report_with_network_finding(repo_name="corr-app")  # identical finding, unchanged
        outcome = await correlate_delivery_finding(store, delivery, new_report)

        assert outcome["status"] == "still_present"
        assert [tuple(k) for k in outcome["still_present_findings"]] == [target]
        assert outcome["resolved_findings"] == []


class TestListDeliveriesPendingFindingCheck:
    async def test_delivery_without_target_findings_never_queued(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="queue-app"))
        await store.create_delivery(
            aid, "queue-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
        )

        pending = await store.list_deliveries_pending_finding_check("queue-app")
        assert pending == []

    async def test_delivery_with_target_findings_is_queued_until_checked(self):
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="queue-app"))
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            aid, "queue-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )

        pending = await store.list_deliveries_pending_finding_check("queue-app")
        assert [p["id"] for p in pending] == [delivery_id]

        await store.update_delivery(delivery_id, finding_resolution="resolved")
        pending_after = await store.list_deliveries_pending_finding_check("queue-app")
        assert pending_after == []

    async def test_partial_delivery_without_pr_url_is_not_queued(self):
        """Failed/partial delivers must not sticky-badge Awaiting verification."""
        store, _ = await make_async_store()
        aid = await store.save(make_report(repo_name="queue-app"))
        target = finding_key("network", "Missing NetworkPolicy")
        await store.create_delivery(
            aid, "queue-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="partial",
            target_findings=[target],
            details={"outcomes": {"cluster_config": {"error": "no GitOps infra repo"}}},
        )

        pending = await store.list_deliveries_pending_finding_check("queue-app")
        assert pending == []


class TestCheckPendingDeliveryVerifications:
    async def test_still_present_outcome_is_persisted_and_logged(self):
        store, _ = await make_async_store()
        old_report = _report_with_network_finding(repo_name="verify-app")
        old_aid = await store.save(old_report)
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            old_aid, "verify-app", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )

        new_report = _report_with_network_finding(repo_name="verify-app")
        new_aid = await store.save(new_report)

        results = await check_pending_delivery_verifications(store, "verify-app", new_report, new_aid)

        assert len(results) == 1
        assert results[0]["status"] == "still_present"
        delivery = await store.get_delivery(delivery_id)
        assert delivery["finding_resolution"] == "still_present"

        events = await store.list_events(target_app="verify-app")
        matching = [e for e in events if e["action"] == "delivery-finding-still-present"]
        assert len(matching) == 1
        assert delivery_id in matching[0]["summary"]

    async def test_still_present_attributes_only_contract_skill_not_companions(self):
        """Source-patch still_present must not blast-reject every onboarding skill.

        Dogfood: migration/container theater PRs left findings present, then
        check_pending_delivery_verifications rejected pdb/limitrange/etc. on
        Decisions even though those skills were never in the delivery.
        """
        store, _ = await make_async_store()
        old_report = make_report(repo_name="attr-app")
        old_report.scores = [
            DimensionScore(
                dimension="data_governance", score=40, max_score=100,
                findings=[Finding(
                    category="migration", severity=Severity.medium,
                    description="no database migration tooling detected",
                    recommendation="Add Alembic",
                )],
            ),
        ]
        old_aid = await store.save(old_report)
        # Companion skill YAML on the assessment (as Scan saves them) —
        # must NOT become skill_effectiveness rejects for this delivery.
        await store.save_onboarding(old_aid, [
            {"category": "codechange", "path": "patch-alembic-ini", "content": "x"},
            {"category": "skills", "path": "attr-app-pdb.yaml", "content": "kind: PodDisruptionBudget"},
            {"category": "skills", "path": "attr-app-limitrange.yaml", "content": "kind: LimitRange"},
            {"category": "skills", "path": "attr-app-security-context.yaml", "content": "kind: SecurityContextConstraints"},
        ])
        target = finding_key("migration", "no database migration tooling detected")
        await store.create_delivery(
            old_aid, "attr-app", {"source_patch": 1}, "source_patch:source-repo-pr",
            status="delivered", target_findings=[target],
            details={"outcomes": {"source_patch": {"pr_url": "https://github.com/example/app/pull/1"}}},
        )
        new_report = make_report(repo_name="attr-app")
        new_report.scores = old_report.scores
        new_aid = await store.save(new_report)

        await check_pending_delivery_verifications(store, "attr-app", new_report, new_aid)

        rows = await store.get_recent_skill_activity(limit=50)
        app_rows = [r for r in rows if r["app_name"] == "attr-app"]
        assert [(r["skill_name"], r["outcome"]) for r in app_rows] == [
            ("db-migration-tooling", "rejected"),
        ]
        assert "finding still present after merge" in (app_rows[0].get("reason") or "")

    async def test_resolved_outcome_is_persisted_and_logged(self):
        store, _ = await make_async_store()
        old_report = _report_with_network_finding(repo_name="verify-app2")
        old_aid = await store.save(old_report)
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            old_aid, "verify-app2", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )

        new_report = _report_with_unrelated_finding(repo_name="verify-app2")
        new_aid = await store.save(new_report)

        results = await check_pending_delivery_verifications(store, "verify-app2", new_report, new_aid)

        assert len(results) == 1
        assert results[0]["status"] == "resolved"
        delivery = await store.get_delivery(delivery_id)
        assert delivery["finding_resolution"] == "resolved"

        events = await store.list_events(target_app="verify-app2")
        matching = [e for e in events if e["action"] == "delivery-finding-resolved"]
        assert len(matching) == 1

    async def test_already_checked_delivery_is_not_reprocessed(self):
        store, _ = await make_async_store()
        old_report = _report_with_network_finding(repo_name="verify-app3")
        old_aid = await store.save(old_report)
        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            old_aid, "verify-app3", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )
        new_report = _report_with_network_finding(repo_name="verify-app3")
        new_aid = await store.save(new_report)

        first = await check_pending_delivery_verifications(store, "verify-app3", new_report, new_aid)
        assert len(first) == 1
        assert first[0]["delivery_id"] == delivery_id

        # AutoMode has been removed -- Phase 4 is now unconditional, so the
        # still-present outcome above also re-dispatched a fresh delivery
        # attempt for the same finding (below FINDING_ESCALATION_THRESHOLD).
        # That new delivery is the one still awaiting its own finding-check
        # now -- the ORIGINAL delivery specifically must never be
        # reprocessed.
        second = await check_pending_delivery_verifications(store, "verify-app3", new_report, new_aid)
        assert delivery_id not in {r["delivery_id"] for r in second}

    async def test_still_present_ledger_card_is_visible(self):
        store, _ = await make_async_store()
        old_report = _report_with_network_finding(repo_name="verify-app4")
        old_aid = await store.save(old_report)
        target = finding_key("network", "Missing NetworkPolicy")
        await store.create_delivery(
            old_aid, "verify-app4", {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT, status="delivered",
            target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )
        new_report = _report_with_network_finding(repo_name="verify-app4")
        new_aid = await store.save(new_report)

        await check_pending_delivery_verifications(store, "verify-app4", new_report, new_aid)

        cards = await get_ledger_cards(store, target_app="verify-app4")
        assert any(c["title"] == "delivery-finding-still-present" and c["card_type"] == "I" for c in cards)


class TestWebhookWiring:
    async def test_push_triggered_reassessment_correlates_prior_delivery(self, portal_client):
        """End-to-end through the real `/api/webhook/github-push` route:
        a prior delivery's target finding, unchanged in the new push-
        triggered re-assessment, is automatically detected as still
        present -- with no human or auto_mode involvement needed for this
        Phase 3 correlation+logging step."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url

        target = finding_key("network", "Missing NetworkPolicy")
        delivery_id = await store.create_delivery(
            old_aid, old_report.repo_name, {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT,
            status="delivered", target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )

        new_report = _report_with_network_finding(repo_name=old_report.repo_name, repo_url=repo_url)

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report):
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 200
        delivery = await store.get_delivery(delivery_id)
        assert delivery["finding_resolution"] == "still_present"

    async def test_below_threshold_failure_redispatches_not_escalates(self, portal_client):
        """AutoMode has been removed, so there is no more `auto_mode`
        toggle gating Phase 4's reaction at all -- a still-present target
        finding below FINDING_ESCALATION_THRESHOLD always re-dispatches a
        fresh fix attempt now (never immediately escalates), exactly like
        `check_pending_delivery_verifications()` always did once auto_mode
        was on."""
        client, store, old_aid = portal_client
        old_report = await store.get(old_aid)
        repo_url = old_report.repo_url

        target = finding_key("network", "Missing NetworkPolicy")
        await store.create_delivery(
            old_aid, old_report.repo_name, {"cluster_config": 1}, MECHANISM_INFRA_REPO_COMMIT,
            status="delivered", target_findings=[target],
            details=_PR_OUTCOME_DETAILS,
        )
        # No prior confirmed failures on record yet -- below threshold.
        new_report = _report_with_network_finding(repo_name=old_report.repo_name, repo_url=repo_url)

        with patch("agentit.portal.routes.webhooks.clone_assess_cleanup", return_value=new_report):
            resp = await client.post(
                "/api/webhook/github-push",
                json=_push_body(repo_url),
                headers={"X-GitHub-Event": "push"},
            )

        assert resp.status_code == 200
        events = await store.list_events(target_app=old_report.repo_name, limit=50)
        # Escalation would fire a real "finding-escalated" event (the
        # `gates` table/generic gate-resolution machinery has been removed
        # entirely, 2026-07-19) -- below threshold, it must redispatch
        # instead, never escalate.
        assert not any(e["action"] == "finding-escalated" for e in events)
        assert any(e["action"] == "finding-redispatched" for e in events)
