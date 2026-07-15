"""Tests for llm_decisions.py: merging fix-review and auto-mode decisions
into one attributed view (by agent/skill, not by human user)."""

from __future__ import annotations

from agentit.llm_decisions import (
    DECISION_TYPE_AUTO_MODE,
    DECISION_TYPE_CAPABILITY_PROPOSAL,
    DECISION_TYPE_FIX_REVIEW,
    DECISION_TYPE_SECRET_CLASSIFY,
    build_secret_classify_events,
    list_llm_decisions,
    summarize_by_attribution,
)
from conftest import make_store


class TestFixReviewDecisions:
    def test_skill_effectiveness_row_becomes_a_decision(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "my-app", "approved", "Fix is correct and safe")

        decisions = list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_FIX_REVIEW
        assert d["attribution"] == "network-policy"
        assert d["attribution_kind"] == "skill"
        assert d["target_app"] == "my-app"
        assert d["outcome"] == "approved"
        assert d["reason"] == "Fix is correct and safe"

    def test_missing_reason_defaults_to_empty_string(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "my-app", "rejected")

        decisions = list_llm_decisions(store)
        assert decisions[0]["reason"] == ""


class TestListEventsByAction:
    """store.list_events_by_action() — the primitive _auto_mode_decisions() relies on."""

    def test_returns_only_events_with_matching_action(self) -> None:
        store = make_store()
        store.log_event("auto-mode", "decision", "app-a", "info", "AUTO-APPLY: safe")
        store.log_event("dispatcher", "gated", "app-a", "info", "unrelated event")

        events = store.list_events_by_action("decision")
        assert len(events) == 1
        assert events[0]["action"] == "decision"

    def test_no_matches_returns_empty_list(self) -> None:
        store = make_store()
        assert store.list_events_by_action("decision") == []

    def test_respects_limit(self) -> None:
        store = make_store()
        for i in range(5):
            store.log_event("auto-mode", "decision", f"app-{i}", "info", "AUTO-APPLY: safe")
        assert len(store.list_events_by_action("decision", limit=2)) == 2


class TestAutoModeDecisions:
    def test_auto_apply_event_becomes_a_decision(self) -> None:
        store = make_store()
        store.log_event("auto-mode", "decision", "my-app", "info",
                         "AUTO-APPLY: LLM classified as safe (0.95): Adds a ConfigMap")

        decisions = list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_AUTO_MODE
        assert d["attribution"] == "auto-mode"
        assert d["attribution_kind"] == "component"
        assert d["target_app"] == "my-app"
        assert d["outcome"] == "auto-applied"
        assert d["reason"] == "LLM classified as safe (0.95): Adds a ConfigMap"

    def test_gate_event_becomes_a_decision(self) -> None:
        store = make_store()
        store.log_event("auto-mode", "decision", "my-app", "warning",
                         "GATE: LLM flagged as destructive: Deletes a Deployment")

        decisions = list_llm_decisions(store)
        d = decisions[0]
        assert d["outcome"] == "gated"
        assert d["reason"] == "LLM flagged as destructive: Deletes a Deployment"

    def test_real_agent_attribution_when_caller_supplied_one(self) -> None:
        """When AutoMode.execute() is called with agent_name=..., the decision
        event's agent_id is the real originating agent, not 'auto-mode'."""
        store = make_store()
        store.log_event("HardeningAgent", "decision", "my-app", "info",
                         "AUTO-APPLY: LLM classified as safe (0.9): Adds NetworkPolicy")

        decisions = list_llm_decisions(store)
        d = decisions[0]
        assert d["attribution"] == "HardeningAgent"
        assert d["attribution_kind"] == "agent"

    def test_other_events_with_different_action_are_not_included(self) -> None:
        store = make_store()
        store.log_event("auto-mode", "auto-applied", "my-app", "info", "Applied 2 manifests")
        store.log_event("dispatcher", "gated", "my-app", "info", "Gated for review")

        decisions = list_llm_decisions(store)
        assert decisions == []


class TestCapabilityProposalDecisions:
    """`capability-run` events (docs/self-improvement-for-agentit.md) →
    decisions attributed to the generic `capability-scout` component --
    every cycle becomes a decision, whether or not it actually proposed
    anything."""

    def test_proposed_cycle_with_pr_url_becomes_a_proposed_decision(self) -> None:
        store = make_store()
        store.log_event(
            "capability-scout", "capability-run", None, "info",
            "Opened proposal PR: Track stack signatures (https://github.com/org/agentit/pull/9)",
            details={"evidence": "README.md:42 — Documented future idea", "pr_url": "https://github.com/org/agentit/pull/9",
                      "gate_results": [{"name": "diff-size", "passed": True}]},
        )

        decisions = list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_CAPABILITY_PROPOSAL
        assert d["attribution"] == "capability-scout"
        assert d["attribution_kind"] == "component"
        assert d["target_app"] == "agentit"
        assert d["outcome"] == "proposed"
        assert d["reason"] == "README.md:42 — Documented future idea"

    def test_gate_blocked_cycle_becomes_a_gate_blocked_decision(self) -> None:
        store = make_store()
        store.log_event(
            "capability-scout", "capability-run", None, "warning",
            "Proposal 'Foo' gate-blocked: test-plan-required",
            details={"evidence": "some evidence", "pr_url": None,
                      "gate_results": [{"name": "test-plan-required", "passed": False}]},
        )

        decisions = list_llm_decisions(store)
        assert decisions[0]["outcome"] == "gate-blocked"

    def test_no_signal_cycle_becomes_a_no_signal_decision(self) -> None:
        store = make_store()
        store.log_event(
            "capability-scout", "capability-run", None, "warning",
            "No proposal this cycle — insufficient real signal.",
            details={"evidence": "", "pr_url": None, "gate_results": []},
        )

        decisions = list_llm_decisions(store)
        assert decisions[0]["outcome"] == "no-signal"

    def test_error_cycle_becomes_an_error_decision(self) -> None:
        store = make_store()
        store.log_event(
            "capability-scout", "capability-run", None, "error",
            "capability-scout run failed: no credentials",
            details={"evidence": "", "pr_url": None, "gate_results": [], "error": "no credentials"},
        )

        decisions = list_llm_decisions(store)
        assert decisions[0]["outcome"] == "error"

    def test_included_alongside_fix_review_and_auto_mode_decisions(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        store.log_event("auto-mode", "decision", "app-b", "info", "AUTO-APPLY: safe: fine")
        store.log_event(
            "capability-scout", "capability-run", None, "info", "Opened proposal PR",
            details={"evidence": "e", "pr_url": "https://github.com/org/agentit/pull/1"},
        )

        decisions = list_llm_decisions(store)
        assert len(decisions) == 3
        types = {d["decision_type"] for d in decisions}
        assert types == {DECISION_TYPE_FIX_REVIEW, DECISION_TYPE_AUTO_MODE, DECISION_TYPE_CAPABILITY_PROPOSAL}

    def test_filter_by_capability_proposal_attribution(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        store.log_event(
            "capability-scout", "capability-run", None, "info", "Opened proposal PR",
            details={"evidence": "e", "pr_url": "https://github.com/org/agentit/pull/1"},
        )

        decisions = list_llm_decisions(store, attribution="capability-scout")
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

    def test_kept_event_becomes_a_kept_decision(self) -> None:
        store = make_store()
        for ev in build_secret_classify_events(
            [{"file_path": "config.py", "secret_type": "api_key", "is_secret": True,
              "confidence": 0.9, "reason": "Looks like a real API key", "kept": True}],
            target_app="my-app",
        ):
            store.log_event(**ev)

        decisions = list_llm_decisions(store)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision_type"] == DECISION_TYPE_SECRET_CLASSIFY
        assert d["attribution"] == "security-analyzer"
        assert d["attribution_kind"] == "component"
        assert d["target_app"] == "my-app"
        assert d["outcome"] == "kept"
        assert d["reason"] == "Looks like a real API key"

    def test_dropped_event_becomes_a_dropped_decision(self) -> None:
        store = make_store()
        for ev in build_secret_classify_events(
            [{"file_path": "app.py", "secret_type": "password", "is_secret": False,
              "confidence": 0.95, "reason": "Reads from an env var lookup", "kept": False}],
            target_app="my-app",
        ):
            store.log_event(**ev)

        decisions = list_llm_decisions(store)
        assert decisions[0]["outcome"] == "dropped"

    def test_included_alongside_other_decision_types(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        store.log_event("auto-mode", "decision", "app-b", "info", "AUTO-APPLY: safe: fine")
        for ev in build_secret_classify_events(
            [{"file_path": "f.py", "secret_type": "token", "is_secret": True,
              "confidence": 0.8, "reason": "real token", "kept": True}],
            target_app="app-c",
        ):
            store.log_event(**ev)

        decisions = list_llm_decisions(store)
        types = {d["decision_type"] for d in decisions}
        assert types == {DECISION_TYPE_FIX_REVIEW, DECISION_TYPE_AUTO_MODE, DECISION_TYPE_SECRET_CLASSIFY}

    def test_filter_by_secret_classify_attribution(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        for ev in build_secret_classify_events(
            [{"file_path": "f.py", "secret_type": "token", "is_secret": True,
              "confidence": 0.8, "reason": "real token", "kept": True}],
            target_app="app-c",
        ):
            store.log_event(**ev)

        decisions = list_llm_decisions(store, attribution="security-analyzer")
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == DECISION_TYPE_SECRET_CLASSIFY


class TestListLlmDecisionsMerging:
    def test_merges_both_decision_types_sorted_newest_first(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
        store.log_event("auto-mode", "decision", "app-b", "info", "AUTO-APPLY: safe: fine")

        decisions = list_llm_decisions(store)
        assert len(decisions) == 2
        types = {d["decision_type"] for d in decisions}
        assert types == {DECISION_TYPE_FIX_REVIEW, DECISION_TYPE_AUTO_MODE}

    def test_filter_by_decision_type(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
        store.log_event("auto-mode", "decision", "app-b", "info", "AUTO-APPLY: safe: fine")

        decisions = list_llm_decisions(store, decision_type=DECISION_TYPE_FIX_REVIEW)
        assert len(decisions) == 1
        assert decisions[0]["decision_type"] == DECISION_TYPE_FIX_REVIEW

    def test_filter_by_attribution(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "looks fine")
        store.record_skill_outcome("containerfile", "app-a", "rejected", "wrong base image")

        decisions = list_llm_decisions(store, attribution="containerfile")
        assert len(decisions) == 1
        assert decisions[0]["attribution"] == "containerfile"

    def test_no_decisions_returns_empty_list(self) -> None:
        store = make_store()
        assert list_llm_decisions(store) == []


class TestSummarizeByAttribution:
    def test_groups_and_counts_outcomes_per_attribution(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        store.record_skill_outcome("network-policy", "app-b", "approved", "fine")
        store.record_skill_outcome("network-policy", "app-c", "rejected", "wrong port")

        decisions = list_llm_decisions(store)
        summary = summarize_by_attribution(decisions)

        assert len(summary) == 1
        g = summary[0]
        assert g["attribution"] == "network-policy"
        assert g["decision_type"] == DECISION_TYPE_FIX_REVIEW
        assert g["total"] == 3
        assert g["outcomes"] == {"approved": 2, "rejected": 1}

    def test_sorted_by_total_descending(self) -> None:
        store = make_store()
        store.record_skill_outcome("network-policy", "app-a", "approved", "fine")
        store.record_skill_outcome("containerfile", "app-a", "approved", "fine")
        store.record_skill_outcome("containerfile", "app-b", "rejected", "no")

        decisions = list_llm_decisions(store)
        summary = summarize_by_attribution(decisions)

        assert summary[0]["attribution"] == "containerfile"
        assert summary[0]["total"] == 2
        assert summary[1]["attribution"] == "network-policy"
        assert summary[1]["total"] == 1

    def test_empty_decisions_produce_empty_summary(self) -> None:
        assert summarize_by_attribution([]) == []
