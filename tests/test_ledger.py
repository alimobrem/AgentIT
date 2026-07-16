"""Tests for the Ledger backing query (docs/ledger-design-spec.md Phase 1):
get_ledger_cards() unions events + gates + deliveries + fix-review decisions
into the card shapes from the spec's §1 table, scoped to one app or
fleet-wide. Also covers the §2 noise-at-scale shaping (tick collapsing,
grouping, the watcher-failure signal) and the §4 rewind chain query."""
from __future__ import annotations

from agentit.ledger import (
    get_chain_cards,
    get_ledger_cards,
    group_cards_by_app,
    recent_watcher_failures,
)
from conftest import make_async_store, make_report


class TestEventCards:
    async def test_assessment_complete_becomes_card_type_a(self):
        async_store, store = await make_async_store()
        await store.log_event(
            "self-assess", "assessment-complete", "app1", "info", "Assessment complete: 80/100",
        )

        cards = await get_ledger_cards(async_store)

        a_cards = [c for c in cards if c["card_type"] == "A"]
        assert len(a_cards) == 1
        assert a_cards[0]["target_app"] == "app1"
        assert a_cards[0]["source"] == "events"

    async def test_tick_complete_becomes_card_type_h(self):
        async_store, store = await make_async_store()
        await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")

        cards = await get_ledger_cards(async_store)

        assert any(c["card_type"] == "H" for c in cards)

    async def test_unrecognized_action_produces_no_card(self):
        """An event action with no spec-defined card type is dropped, not
        rendered as an unlabeled generic card."""
        async_store, store = await make_async_store()
        await store.log_event("some-agent", "some-unmapped-action", "app1", "info", "n/a")

        cards = await get_ledger_cards(async_store)

        assert cards == []

    async def test_rollback_recommended_is_card_type_j(self):
        """Phase 0's new log_event call (slo_tracker.py) feeds this directly."""
        async_store, store = await make_async_store()
        await store.log_event(
            "slo-tracker", "rollback-recommended", "app1", "critical", "SLO breach -- rollback?",
        )

        cards = await get_ledger_cards(async_store)

        j_cards = [c for c in cards if c["card_type"] == "J"]
        assert len(j_cards) == 1
        assert j_cards[0]["severity"] == "critical"

    async def test_drift_auto_synced_is_card_type_k(self):
        """Phase 0's new log_event call (drift_detector.py) feeds this directly."""
        async_store, store = await make_async_store()
        await store.log_event("drift-detector", "drift-auto-synced", "app1", "info", "synced")

        cards = await get_ledger_cards(async_store)

        assert any(c["card_type"] == "K" for c in cards)


class TestGateCards:
    async def test_pending_gate_is_card_type_d(self):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app2"))
        await store.create_gate(aid, "finding-security", "Review this finding")

        cards = await get_ledger_cards(async_store, target_app="app2", assessment_id=aid)

        d_cards = [c for c in cards if c["card_type"] == "D"]
        assert len(d_cards) == 1
        assert d_cards[0]["target_app"] == "app2"
        assert d_cards[0]["gate_status"] == "pending"

    async def test_resolved_gate_is_card_type_e(self):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app3"))
        gate_id = await store.create_gate(aid, "finding-security", "Review this finding")
        await store.resolve_gate(gate_id, "approved", "alice")

        cards = await get_ledger_cards(async_store, target_app="app3", assessment_id=aid)

        e_cards = [c for c in cards if c["card_type"] == "E"]
        assert len(e_cards) == 1
        assert e_cards[0]["gate_status"] == "approved"

    async def test_global_view_resolves_app_name_via_join(self):
        """The fleet-wide path uses list_all_gates(), which already joins
        app_name -- unlike list_gates_for_assessment(), which doesn't."""
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app4"))
        await store.create_gate(aid, "finding-security", "Review this finding")

        cards = await get_ledger_cards(async_store)

        d_cards = [c for c in cards if c["card_type"] == "D"]
        assert any(c["target_app"] == "app4" for c in d_cards)


class TestDeliveryCards:
    async def test_delivery_is_card_type_f(self):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app5"))
        await store.create_delivery(aid, "app5", {"cluster_config": ["a.yaml"]}, "direct-apply")

        cards = await get_ledger_cards(async_store, target_app="app5", assessment_id=aid)

        f_cards = [c for c in cards if c["card_type"] == "F"]
        assert len(f_cards) == 1
        assert f_cards[0]["mechanism"] == "direct-apply"

    async def test_global_view_uses_list_all_deliveries(self):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="app6"))
        await store.create_delivery(aid, "app6", {}, "infra-repo-commit")

        cards = await get_ledger_cards(async_store)

        assert any(c["card_type"] == "F" and c["target_app"] == "app6" for c in cards)


class TestFixReviewCards:
    async def test_fix_review_outcome_is_card_type_g(self):
        async_store, store = await make_async_store()
        await store.record_skill_outcome("security-secrets-fix", "app7", "approved", "looks good")

        cards = await get_ledger_cards(async_store)

        g_cards = [c for c in cards if c["card_type"] == "G"]
        assert len(g_cards) == 1
        assert g_cards[0]["attribution"] == "security-secrets-fix"
        assert g_cards[0]["target_app"] == "app7"

    async def test_per_app_scope_filters_fix_reviews_by_app(self):
        async_store, store = await make_async_store()
        await store.record_skill_outcome("skill-a", "app8", "approved", "")
        await store.record_skill_outcome("skill-b", "other-app", "approved", "")

        cards = await get_ledger_cards(async_store, target_app="app8")

        g_cards = [c for c in cards if c["card_type"] == "G"]
        assert len(g_cards) == 1
        assert g_cards[0]["target_app"] == "app8"


class TestOrderingAndScope:
    async def test_cards_sorted_newest_first(self):
        async_store, store = await make_async_store()
        await store.log_event("a", "assessment-complete", "app9", "info", "first")
        await store.log_event("a", "tick-complete", None, "info", "second")

        cards = await get_ledger_cards(async_store)

        timestamps = [c["timestamp"] for c in cards]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_per_app_scope_excludes_other_apps_events(self):
        async_store, store = await make_async_store()
        await store.log_event("a", "assessment-complete", "app-mine", "info", "mine")
        await store.log_event("a", "assessment-complete", "app-other", "info", "not mine")

        cards = await get_ledger_cards(async_store, target_app="app-mine")

        assert all(c.get("target_app") in (None, "app-mine") for c in cards)


class TestTickCollapsing:
    """docs/ledger-design-spec.md §2 rule 4: consecutive tick-complete
    events from the same watcher collapse into one summary card; a single
    tick-failed breaks the collapse and always renders as its own card."""

    async def test_consecutive_tick_completes_collapse_into_one_card(self):
        async_store, store = await make_async_store()
        for _ in range(3):
            await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")

        cards = await get_ledger_cards(async_store)

        h_cards = [c for c in cards if c["card_type"] == "H"]
        assert len(h_cards) == 1
        assert "3 clean ticks" in h_cards[0]["summary"]
        assert "vuln-watcher" in h_cards[0]["summary"]

    async def test_single_tick_complete_is_not_summarized(self):
        """A run of exactly one tick is returned as-is -- no point
        summarizing "1 clean tick since <its own timestamp>"."""
        async_store, store = await make_async_store()
        await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")

        cards = await get_ledger_cards(async_store)

        h_cards = [c for c in cards if c["card_type"] == "H"]
        assert len(h_cards) == 1
        assert h_cards[0]["summary"] == "tick ok"

    async def test_tick_failed_breaks_the_collapse(self):
        async_store, store = await make_async_store()
        await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")
        await store.log_event("vuln-watcher", "tick-failed", None, "error", "boom")
        await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")

        cards = await get_ledger_cards(async_store)

        h_cards = [c for c in cards if c["card_type"] == "H"]
        # Neither run around the failure is long enough to collapse (1 each) --
        # all three ticks stay individually visible, the failure never hidden.
        assert len(h_cards) == 3
        assert sum(1 for c in h_cards if c["title"] == "tick-failed") == 1

    async def test_different_watchers_never_collapse_together(self):
        async_store, store = await make_async_store()
        await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")
        await store.log_event("drift-detector", "tick-complete", None, "info", "tick ok")

        cards = await get_ledger_cards(async_store)

        h_cards = [c for c in cards if c["card_type"] == "H"]
        assert len(h_cards) == 2


class TestChainCountAnnotation:
    async def test_events_sharing_a_correlation_id_get_a_chain_count(self):
        async_store, store = await make_async_store()
        await store.log_event(
            "orchestrator", "assessment-complete", "chain-app", "info", "80/100",
            correlation_id="chain-abc",
        )
        await store.log_event(
            "orchestrator", "fix-generated", "chain-app", "info", "generated a fix",
            correlation_id="chain-abc",
        )

        cards = await get_ledger_cards(async_store)

        chained = [c for c in cards if c.get("correlation_id") == "chain-abc"]
        assert len(chained) == 2
        assert all(c["chain_count"] == 2 for c in chained)

    async def test_a_lone_event_has_a_chain_count_of_one(self):
        """A chain of exactly one is real (it counts itself) but not worth
        a "Part of a chain" affordance -- the template only renders that
        once chain_count > 1, which this asserts the data supports."""
        async_store, store = await make_async_store()
        await store.log_event(
            "orchestrator", "assessment-complete", "lone-app", "info", "80/100",
            correlation_id="only-one",
        )

        cards = await get_ledger_cards(async_store)

        card = next(c for c in cards if c.get("correlation_id") == "only-one")
        assert card["chain_count"] == 1

    async def test_gate_and_delivery_cards_have_no_chain_count(self):
        """Per spec §1, correlation_id is an events-only column -- gates and
        deliveries never carry one, so they never claim a chain size."""
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="no-chain-app"))
        await store.create_gate(aid, "finding-security", "Review this finding")

        cards = await get_ledger_cards(async_store, target_app="no-chain-app", assessment_id=aid)

        d_card = next(c for c in cards if c["card_type"] == "D")
        assert "chain_count" not in d_card


class TestGroupCardsByApp:
    def test_groups_preserve_newest_first_order_per_app(self):
        cards = [
            {"target_app": "app1", "timestamp": "2026-01-03T00:00:00"},
            {"target_app": "app2", "timestamp": "2026-01-02T00:00:00"},
            {"target_app": "app1", "timestamp": "2026-01-01T00:00:00"},
        ]

        grouped = group_cards_by_app(cards)

        assert list(grouped.keys()) == ["app1", "app2"]
        assert [c["timestamp"] for c in grouped["app1"]] == ["2026-01-03T00:00:00", "2026-01-01T00:00:00"]

    def test_cards_with_no_target_app_are_dropped(self):
        cards = [{"target_app": None, "timestamp": "2026-01-01T00:00:00"}]

        assert group_cards_by_app(cards) == {}


class TestRecentWatcherFailures:
    async def test_a_recent_tick_failed_is_flagged(self):
        async_store, store = await make_async_store()
        await store.log_event("vuln-watcher", "tick-failed", None, "error", "boom")

        cards = await get_ledger_cards(async_store)

        alerts = recent_watcher_failures(cards, hours=4)
        assert len(alerts) == 1
        assert alerts[0]["agent_id"] == "vuln-watcher"

    async def test_tick_complete_is_never_flagged(self):
        async_store, store = await make_async_store()
        await store.log_event("vuln-watcher", "tick-complete", None, "info", "tick ok")

        cards = await get_ledger_cards(async_store)

        assert recent_watcher_failures(cards, hours=4) == []

    def test_an_old_failure_outside_the_window_is_not_flagged(self):
        old_card = {
            "card_type": "H", "title": "tick-failed", "agent_id": "vuln-watcher",
            "timestamp": "2000-01-01T00:00:00+00:00",
        }
        assert recent_watcher_failures([old_card], hours=4) == []


class TestChainCards:
    """docs/ledger-design-spec.md §4: the rewind scrubber's backing query --
    reuses list_events_by_correlation_id (the exact same query the Events
    page's existing "Chain" link runs) plus the gates/deliveries/fix-review
    rows for whichever apps the chain touched."""

    async def test_chain_includes_events_and_the_related_gate(self):
        async_store, store = await make_async_store()
        aid = await store.save(make_report(repo_name="chain-app"))
        await store.create_gate(aid, "finding-security", "Review this finding")

        # store.save() itself logs "assessment-complete" with
        # correlation_id=assessment_id -- the real chain this app already has.
        cards = await get_chain_cards(async_store, aid)

        card_types = {c["card_type"] for c in cards}
        assert "A" in card_types  # assessment-complete
        assert "D" in card_types  # the pending gate
        timestamps = [c["timestamp"] for c in cards]
        assert timestamps == sorted(timestamps)  # oldest first, for scrubbing forward in time

    async def test_unknown_correlation_id_returns_no_cards(self):
        async_store, store = await make_async_store()
        cards = await get_chain_cards(async_store, "does-not-exist")
        assert cards == []
