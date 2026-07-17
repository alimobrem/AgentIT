"""Tests for `verify_and_close_delivery()`'s (`delivery.py`) event logging --
docs/onboarding-loop-vision-gap-analysis.md Phase 0 item 3.

Before this fix, `verify_and_close_delivery()` updated a `deliveries` row's
status column (`verified`/`rolled_back`/`breach-reported`) but never called
`log_event()`/`publish_event()`, so a delivery being confirmed healthy or
found to have failed produced no Ledger card and no observable event.
Mirrors `slo_tracker.py`'s `rollback-recommended` event-logging pattern
(Kafka publish + store-persisted event, both best-effort).
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.portal.delivery import (
    MECHANISM_DIRECT_APPLY,
    MECHANISM_INFRA_REPO_COMMIT,
    verify_and_close_delivery,
)
from conftest import make_async_store, make_report


async def _make_delivery(store, mechanism: str) -> tuple[str, str]:
    report = make_report(repo_name="verify-events-app")
    aid = await store.save(report)
    delivery_id = await store.create_delivery(
        aid, report.repo_name, {"cluster_config": 1}, mechanism, status="in_progress",
    )
    return aid, delivery_id


class TestVerifiedOutcomeLogsEvent:
    async def test_healthy_verification_logs_delivery_verified_event(self):
        store, raw = await make_async_store()
        aid, delivery_id = await _make_delivery(store, MECHANISM_DIRECT_APPLY)

        with patch("agentit.remediation_loop.verify_slos", return_value={"healthy": True, "reason": "no breach"}):
            result = await verify_and_close_delivery(
                store, delivery_id, aid, "verify-events-app", "verify-events-app", MECHANISM_DIRECT_APPLY,
            )

        assert result["healthy"] is True
        delivery = await raw.get_delivery(delivery_id)
        assert delivery["status"] == "verified"

        events = await raw.list_events(target_app="verify-events-app")
        matching = [e for e in events if e["action"] == "delivery-verified"]
        assert len(matching) == 1
        assert matching[0]["severity"] == "info"
        assert delivery_id in matching[0]["summary"]


class TestBreachedOutcomesLogEvents:
    async def test_direct_apply_breach_logs_rolled_back_event(self):
        store, raw = await make_async_store()
        aid, delivery_id = await _make_delivery(store, MECHANISM_DIRECT_APPLY)

        with patch("agentit.remediation_loop.verify_slos",
                    return_value={"healthy": False, "reason": "latency_p99_ms breached"}), \
             patch("agentit.remediation_loop.rollback_action",
                   return_value={"outcome": "rolled_back", "details": "rollout undo"}) as mock_rollback:
            result = await verify_and_close_delivery(
                store, delivery_id, aid, "verify-events-app", "verify-events-app", MECHANISM_DIRECT_APPLY,
            )

        assert result["healthy"] is False
        mock_rollback.assert_called_once()
        delivery = await raw.get_delivery(delivery_id)
        assert delivery["status"] == "rolled_back"

        events = await raw.list_events(target_app="verify-events-app")
        matching = [e for e in events if e["action"] == "delivery-rolled-back"]
        assert len(matching) == 1
        assert matching[0]["severity"] == "critical"
        assert "latency_p99_ms breached" in matching[0]["summary"]

    async def test_gitops_breach_logs_breach_reported_event_without_rollback(self):
        """GitOps rollback semantics are explicitly out of scope (design
        doc's "Deliberately not addressed" #3) -- no rollback_action() call,
        but the breach must still be a real, logged, observable event."""
        store, raw = await make_async_store()
        aid, delivery_id = await _make_delivery(store, MECHANISM_INFRA_REPO_COMMIT)

        with patch("agentit.remediation_loop.verify_slos",
                    return_value={"healthy": False, "reason": "error_rate breached"}), \
             patch("agentit.remediation_loop.rollback_action") as mock_rollback:
            result = await verify_and_close_delivery(
                store, delivery_id, aid, "verify-events-app", "verify-events-app", MECHANISM_INFRA_REPO_COMMIT,
            )

        assert result["healthy"] is False
        mock_rollback.assert_not_called()
        delivery = await raw.get_delivery(delivery_id)
        assert delivery["status"] == "breach-reported"

        events = await raw.list_events(target_app="verify-events-app")
        matching = [e for e in events if e["action"] == "delivery-breach-reported"]
        assert len(matching) == 1
        assert matching[0]["severity"] == "critical"
        assert "error_rate breached" in matching[0]["summary"]


class TestLedgerCardMapping:
    async def test_verification_events_map_to_ledger_cards(self):
        """docs/ledger-design-spec.md's own convention: a new log_event()
        action needs a matching `_EVENT_ACTION_TO_CARD_TYPE` entry or it's
        silently dropped from the Ledger stream entirely."""
        from agentit.ledger import get_ledger_cards

        store, raw = await make_async_store()
        aid, delivery_id = await _make_delivery(store, MECHANISM_DIRECT_APPLY)

        with patch("agentit.remediation_loop.verify_slos", return_value={"healthy": True, "reason": "ok"}):
            await verify_and_close_delivery(
                store, delivery_id, aid, "verify-events-app", "verify-events-app", MECHANISM_DIRECT_APPLY,
            )

        cards = await get_ledger_cards(store, target_app="verify-events-app")
        assert any(c["title"] == "delivery-verified" and c["card_type"] == "F" for c in cards)
