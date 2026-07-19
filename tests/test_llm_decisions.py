"""Tests for llm_decisions.py: merging fix-review, secret-classify, and
capability-proposal decisions into one attributed view (by agent/skill, not
by human user)."""

from __future__ import annotations

import asyncio

from agentit.llm_decisions import (
    DECISION_TYPE_CAPABILITY_PROPOSAL,
    DECISION_TYPE_FIX_REVIEW,
    DECISION_TYPE_SECRET_CLASSIFY,
    build_secret_classify_events,
    list_llm_decisions as _list_llm_decisions_sync,
    summarize_by_attribution,
)
from conftest import make_store


async def list_llm_decisions(store, **kwargs):
    """Test-only wrapper: `list_llm_decisions` is a synchronous helper
    meant to be run off the event loop via `asyncio.to_thread` (see its own
    docstring/`routes/insights.py`'s real call site) so its internal
    `_bridge()` can schedule the (now-always-async) store calls back onto
    *this* loop without deadlocking -- calling it directly, in-process,
    from the same coroutine that owns this loop would deadlock instead of
    working, the same way production code never calls it directly either.
    """
    return await asyncio.to_thread(
        _list_llm_decisions_sync, store, loop=asyncio.get_running_loop(), **kwargs,
    )


class TestFixReviewDecisions:
    async def test_skill_effectiveness_row_becomes_a_decision(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "my-app", "approved", "Fix is correct and safe")

        decisions = await list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_FIX_REVIEW
        assert d["attribution"] == "network-policy"
        assert d["attribution_kind"] == "skill"
        assert d["target_app"] == "my-app"
        assert d["outcome"] == "approved"
        assert d["reason"] == "Fix is correct and safe"

    async def test_missing_reason_defaults_to_empty_string(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "my-app", "rejected")

        decisions = await list_llm_decisions(store)
        assert decisions[0]["reason"] == ""


class TestListEventsByAction:
    """store.list_events_by_action() — the generic primitive
    _secret_classify_decisions()/_capability_proposal_decisions() rely on."""

    async def test_returns_only_events_with_matching_action(self) -> None:
        store = await make_store()
        await store.log_event("auto-mode", "decision", "app-a", "info", "AUTO-APPLY: safe")
        await store.log_event("dispatcher", "gated", "app-a", "info", "unrelated event")

        events = await store.list_events_by_action("decision")
        assert len(events) == 1
        assert events[0]["action"] == "decision"

    async def test_no_matches_returns_empty_list(self) -> None:
        store = await make_store()
        assert await store.list_events_by_action("decision") == []

    async def test_respects_limit(self) -> None:
        store = await make_store()
        for i in range(5):
            await store.log_event("auto-mode", "decision", f"app-{i}", "info", "AUTO-APPLY: safe")
        assert len(await store.list_events_by_action("decision", limit=2)) == 2


class TestDecisionActionEventsNoLongerSurfaced:
    """AutoMode (the only caller that ever logged an action='decision'
    event) has been removed -- such events, including any stray
    historical ones from before the removal, must never surface as a
    decision anymore."""

    async def test_decision_action_event_produces_no_decision(self) -> None:
        store = await make_store()
        await store.log_event("auto-mode", "decision", "my-app", "info",
                         "AUTO-APPLY: LLM classified as safe (0.95): Adds a ConfigMap")

        decisions = await list_llm_decisions(store)
        assert decisions == []

    async def test_other_events_with_different_action_are_not_included(self) -> None:
        store = await make_store()
        await store.log_event("auto-mode", "auto-applied", "my-app", "info", "Applied 2 manifests")
        await store.log_event("dispatcher", "gated", "my-app", "info", "Gated for review")

        decisions = await list_llm_decisions(store)
        assert decisions == []


class TestCapabilityProposalDecisions:
    """`capability-run` events (docs/self-improvement-for-agentit.md) →
    decisions attributed to the generic `capability-scout` component --
    every cycle becomes a decision, whether or not it actually proposed
    anything."""

    async def test_proposed_cycle_with_pr_url_becomes_a_proposed_decision(self) -> None:
        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "info",
            "Opened proposal PR: Track stack signatures (https://github.com/org/agentit/pull/9)",
            details={"evidence": "README.md:42 — Documented future idea", "pr_url": "https://github.com/org/agentit/pull/9",
                      "gate_results": [{"name": "diff-size", "passed": True}]},
        )

        decisions = await list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_CAPABILITY_PROPOSAL
        assert d["attribution"] == "capability-scout"
        assert d["attribution_kind"] == "component"
        assert d["target_app"] == "agentit"
        assert d["outcome"] == "proposed"
        assert d["reason"] == "README.md:42 — Documented future idea"

    async def test_gate_blocked_cycle_becomes_a_gate_blocked_decision(self) -> None:
        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "warning",
            "Proposal 'Foo' gate-blocked: test-plan-required",
            details={"evidence": "some evidence", "pr_url": None,
                      "gate_results": [{"name": "test-plan-required", "passed": False}]},
        )

        decisions = await list_llm_decisions(store)
        assert decisions[0]["outcome"] == "gate-blocked"

    async def test_no_signal_cycle_becomes_a_no_signal_decision(self) -> None:
        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "warning",
            "No proposal this cycle — insufficient real signal.",
            details={"evidence": "", "pr_url": None, "gate_results": []},
        )

        decisions = await list_llm_decisions(store)
        assert decisions[0]["outcome"] == "no-signal"

    async def test_error_cycle_becomes_an_error_decision(self) -> None:
        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "error",
            "capability-scout run failed: no credentials",
            details={"evidence": "", "pr_url": None, "gate_results": [], "error": "no credentials"},
        )

        decisions = await list_llm_decisions(store)
        assert decisions[0]["outcome"] == "error"

    async def test_included_alongside_fix_review_decisions(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        await store.log_event(
            "capability-scout", "capability-run", None, "info", "Opened proposal PR",
            details={"evidence": "e", "pr_url": "https://github.com/org/agentit/pull/1"},
        )

        decisions = await list_llm_decisions(store)
        assert len(decisions) == 2
        types = {d["decision_type"] for d in decisions}
        assert types == {DECISION_TYPE_FIX_REVIEW, DECISION_TYPE_CAPABILITY_PROPOSAL}

    async def test_json_shaped_evidence_is_humanized_not_dumped_raw(self) -> None:
        """Regression, confirmed live: the Decisions page's capability-
        proposal "Reasoning" column showed a raw JSON dump (e.g. one of
        `await store.get_agent_stats()`'s own per-agent dicts) while every other
        decision type (fix-review, secret-classify) always shows
        a plain-English sentence. A JSON-object-shaped `evidence` string
        must be reformatted as readable "key: value" text instead."""
        import json as _json

        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "warning",
            "No proposal this cycle — insufficient real signal.",
            details={
                "evidence": _json.dumps({"agent": "remediation-loop", "total_events": 36, "success_rate": 83.3}),
                "pr_url": None, "gate_results": [],
            },
        )

        decisions = await list_llm_decisions(store)
        reason = decisions[0]["reason"]
        assert "{" not in reason and "}" not in reason and '"' not in reason
        assert "remediation-loop" in reason
        assert "36" in reason

    async def test_json_shaped_evidence_list_is_humanized(self) -> None:
        import json as _json

        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "warning",
            "No proposal this cycle.",
            details={
                "evidence": _json.dumps([
                    {"agent": "remediation-loop", "total_events": 36},
                    {"agent": "hardening", "total_events": 12},
                ]),
                "pr_url": None, "gate_results": [],
            },
        )

        decisions = await list_llm_decisions(store)
        reason = decisions[0]["reason"]
        assert "{" not in reason and "}" not in reason
        assert "remediation-loop" in reason
        assert "hardening" in reason

    async def test_plain_prose_evidence_is_left_unchanged(self) -> None:
        """Confirms the humanizer is a no-op for the common, already-good
        case -- real doc-citation evidence text must render verbatim."""
        store = await make_store()
        await store.log_event(
            "capability-scout", "capability-run", None, "info",
            "Opened proposal PR: Track stack signatures (https://github.com/org/agentit/pull/9)",
            details={"evidence": "README.md:42 — Documented future idea",
                      "pr_url": "https://github.com/org/agentit/pull/9",
                      "gate_results": [{"name": "diff-size", "passed": True}]},
        )

        decisions = await list_llm_decisions(store)
        assert decisions[0]["reason"] == "README.md:42 — Documented future idea"

    async def test_filter_by_capability_proposal_attribution(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        await store.log_event(
            "capability-scout", "capability-run", None, "info", "Opened proposal PR",
            details={"evidence": "e", "pr_url": "https://github.com/org/agentit/pull/1"},
        )

        decisions = await list_llm_decisions(store, attribution="capability-scout")
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == DECISION_TYPE_CAPABILITY_PROPOSAL


class TestBuildSecretClassifyEvents:
    """`SecurityAnalyzer`'s raw per-match `classify_secret` verdicts →
    `store.log_event()` kwargs (see `analyzers.security.SecurityAnalyzer`'s
    `secret_decisions_out` param)."""

    def test_kept_decision_becomes_a_kept_prefixed_event(self) -> None:
        events = build_secret_classify_events(
            [{"file_path": "config.py", "secret_type": "api_key", "is_secret": True,
              "confidence": 0.9, "reason": "Looks like a real API key", "kept": True}],
            target_app="my-app",
        )
        assert len(events) == 1
        ev = events[0]
        assert ev["agent_id"] == "security-analyzer"
        assert ev["action"] == "secret-classify"
        assert ev["target_app"] == "my-app"
        assert ev["summary"] == "KEPT: Looks like a real API key"
        assert ev["details"] == {
            "file_path": "config.py", "secret_type": "api_key",
            "is_secret": True, "confidence": 0.9,
        }

    def test_dropped_decision_becomes_a_dropped_prefixed_event(self) -> None:
        events = build_secret_classify_events(
            [{"file_path": "app.py", "secret_type": "password", "is_secret": False,
              "confidence": 0.95, "reason": "Reads from an env var lookup", "kept": False}],
            target_app="my-app",
        )
        assert events[0]["summary"] == "DROPPED: Reads from an env var lookup"

    def test_empty_decisions_produce_no_events(self) -> None:
        assert build_secret_classify_events([], target_app="my-app") == []


class TestSecretClassifyDecisions:
    """`secret-classify` events → decisions attributed to the generic
    `security-analyzer` component -- every real `classify_secret` LLM call
    becomes a decision, whether kept or dropped."""

    async def test_kept_event_becomes_a_kept_decision(self) -> None:
        store = await make_store()
        for ev in build_secret_classify_events(
            [{"file_path": "config.py", "secret_type": "api_key", "is_secret": True,
              "confidence": 0.9, "reason": "Looks like a real API key", "kept": True}],
            target_app="my-app",
        ):
            await store.log_event(**ev)

        decisions = await list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_SECRET_CLASSIFY
        assert d["attribution"] == "security-analyzer"
        assert d["attribution_kind"] == "component"
        assert d["target_app"] == "my-app"
        assert d["outcome"] == "kept"
        assert d["reason"] == "Looks like a real API key"

    async def test_dropped_event_becomes_a_dropped_decision(self) -> None:
        store = await make_store()
        for ev in build_secret_classify_events(
            [{"file_path": "app.py", "secret_type": "password", "is_secret": False,
              "confidence": 0.95, "reason": "Reads from an env var lookup", "kept": False}],
            target_app="my-app",
        ):
            await store.log_event(**ev)

        decisions = await list_llm_decisions(store)
        assert decisions[0]["outcome"] == "dropped"

    async def test_included_alongside_other_decision_types(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        for ev in build_secret_classify_events(
            [{"file_path": "f.py", "secret_type": "token", "is_secret": True,
              "confidence": 0.8, "reason": "real token", "kept": True}],
            target_app="app-c",
        ):
            await store.log_event(**ev)

        decisions = await list_llm_decisions(store)
        types = {d["decision_type"] for d in decisions}
        assert types == {DECISION_TYPE_FIX_REVIEW, DECISION_TYPE_SECRET_CLASSIFY}

    async def test_filter_by_secret_classify_attribution(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        for ev in build_secret_classify_events(
            [{"file_path": "f.py", "secret_type": "token", "is_secret": True,
              "confidence": 0.8, "reason": "real token", "kept": True}],
            target_app="app-c",
        ):
            await store.log_event(**ev)

        decisions = await list_llm_decisions(store, attribution="security-analyzer")
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == DECISION_TYPE_SECRET_CLASSIFY


class TestListLlmDecisionsMerging:
    async def test_merges_both_decision_types_sorted_newest_first(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
        await store.log_event(
            "capability-scout", "capability-run", None, "info", "Opened proposal PR",
            details={"evidence": "e", "pr_url": "https://github.com/org/agentit/pull/1"},
        )

        decisions = await list_llm_decisions(store)
        assert len(decisions) == 2
        types = {d["decision_type"] for d in decisions}
        assert types == {DECISION_TYPE_FIX_REVIEW, DECISION_TYPE_CAPABILITY_PROPOSAL}

    async def test_filter_by_decision_type(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
        await store.log_event(
            "capability-scout", "capability-run", None, "info", "Opened proposal PR",
            details={"evidence": "e", "pr_url": "https://github.com/org/agentit/pull/1"},
        )

        decisions = await list_llm_decisions(store, decision_type=DECISION_TYPE_FIX_REVIEW)
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == DECISION_TYPE_FIX_REVIEW

    async def test_filter_by_attribution(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
        await store.record_skill_outcome("containerfile", "app-a", "rejected", "wrong base image")

        decisions = await list_llm_decisions(store, attribution="containerfile")
        assert len(decisions) == 1
        assert decisions[0]["attribution"] == "containerfile"

    async def test_no_decisions_returns_empty_list(self) -> None:
        store = await make_store()
        assert await list_llm_decisions(store) == []


class TestSummarizeByAttribution:
    async def test_groups_and_counts_outcomes_per_attribution(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        await store.record_skill_outcome("network-policy", "app-b", "approved", "fine")
        await store.record_skill_outcome("network-policy", "app-c", "rejected", "wrong port")

        decisions = await list_llm_decisions(store)
        summary = summarize_by_attribution(decisions)

        assert len(summary) == 1
        g = summary[0]
        assert g["attribution"] == "network-policy"
        assert g["decision_type"] == DECISION_TYPE_FIX_REVIEW
        assert g["total"] == 3
        assert g["outcomes"] == {"approved": 2, "rejected": 1}

    async def test_sorted_by_total_descending(self) -> None:
        store = await make_store()
        await store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        await store.record_skill_outcome("containerfile", "app-a", "approved", "fine")
        await store.record_skill_outcome("containerfile", "app-b", "rejected", "no")

        decisions = await list_llm_decisions(store)
        summary = summarize_by_attribution(decisions)

        assert summary[0]["attribution"] == "containerfile"
        assert summary[0]["total"] == 2
        assert summary[1]["attribution"] == "network-policy"
        assert summary[1]["total"] == 1

    def test_empty_decisions_produce_empty_summary(self) -> None:
        assert summarize_by_attribution([]) == []
