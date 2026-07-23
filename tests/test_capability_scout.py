"""Tests for capability_scout.py's pure research/propose/gate logic --
the counterpart to test_learning_agent.py, but for the loop that proposes
changes to AgentIT's own codebase rather than the skills catalog. See
docs/self-improvement-for-agentit.md and tests/test_capability_scout_watcher.py
for the watcher class itself."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentit.capability_scout import (
    CAPABILITY_OUTCOME_ACTION,
    CAPABILITY_RUN_ACTION,
    MAX_DIFF_FILES,
    MAX_DIFF_LINES,
    MIN_SIGNAL_ROWS,
    build_diff,
    check_diff_size,
    check_evidence_usefulness,
    check_has_test_plan,
    check_no_open_self_improve_pr,
    check_no_secrets,
    check_scope_allowlist,
    check_syntax,
    describe_capability_run,
    fetch_pr_close_comments,
    filter_actionable_doc_gaps,
    gather_evidence,
    last_merge_broke_ci,
    list_store_capabilities,
    outcome_from_pr_status,
    parse_reject_reason,
    proposal_already_implemented,
    proposal_blocked_by_outcome,
    rank_doc_gaps,
    recent_capability_titles,
    render_proposal_doc,
    run_safety_gates,
    run_test_suite,
    scan_doc_gaps,
    slugify,
    sync_proposal_outcomes,
)
from conftest import make_async_store


def _proposal(**overrides) -> dict:
    base = {
        "has_proposal": True,
        "title": "Track stack signatures",
        "gap_description": "README documents an idea that was never built",
        # Cite a dogfood/finding signal so evidence-usefulness gate passes.
        "evidence": (
            "Dogfood Scan finding still_present after merge — README.md:42 "
            "documents a future idea that never cleared the finding"
        ),
        "target_files": ["src/agentit/portal/store.py", "tests/test_store.py"],
        "change_summary": "Add a counter and a threshold check",
        "risk": "low",
        "test_plan": "Assert the threshold logic in a new test",
    }
    base.update(overrides)
    return base


# ── scan_doc_gaps ──────────────────────────────────────────────────────────


class TestScanDocGaps:
    def test_finds_known_gap_anchor(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "example.md").write_text(
            "# Example\n\nKnown gap: we never built the retry logic.\n", encoding="utf-8",
        )
        gaps = scan_doc_gaps(docs)
        assert len(gaps) == 1
        assert gaps[0]["anchor"] == "Known gap"
        assert "retry logic" in gaps[0]["text"]
        assert gaps[0]["line_no"] == 3

    def test_finds_multiple_anchor_phrases(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text(
            "Deliberately deferred — not started.\nDocumented future idea (not built): auto-trigger.\n",
            encoding="utf-8",
        )
        gaps = scan_doc_gaps(docs)
        assert len(gaps) == 2

    def test_never_fabricates_a_gap_with_no_matching_text(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "clean.md").write_text("Everything here is fully implemented.\n", encoding="utf-8")
        assert scan_doc_gaps(docs) == []

    def test_missing_docs_dir_returns_empty_list(self, tmp_path):
        assert scan_doc_gaps(tmp_path / "nonexistent") == []


# ── filter already-implemented / meta gaps ─────────────────────────────────


class TestFilterActionableDocGaps:
    def test_skips_stack_signature_when_module_present(self, tmp_path):
        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "stack_signature_detector.py").write_text("def detect():\n    return []\n", encoding="utf-8")
        gaps = [{
            "file": "docs/self-improvement-for-agentit.md",
            "line_no": 178,
            "anchor": "Documented future idea",
            "text": 'Documented future idea (not built): stack-signature detection logic',
        }]
        assert filter_actionable_doc_gaps(gaps, repo_dir=tmp_path) == []

    def test_keeps_unrelated_gap(self, tmp_path):
        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "stack_signature_detector.py").write_text("x = 1\n", encoding="utf-8")
        gaps = [{
            "file": "docs/ledger-design-spec.md",
            "line_no": 165,
            "anchor": "not built",
            "text": '**Explicitly not built:** the predictive "fast-forward" view',
        }]
        assert len(filter_actionable_doc_gaps(gaps, repo_dir=tmp_path)) == 1

    def test_skips_shipped_marker_and_meta_anchor_lists(self, tmp_path):
        gaps = [
            {
                "file": "docs/x.md",
                "line_no": 1,
                "anchor": "Known gap",
                "text": "~~scanner~~ — **shipped**: Do not re-propose this as a gap.",
            },
            {
                "file": "docs/x.md",
                "line_no": 2,
                "anchor": "Known gap",
                "text": 'Prefer "Known gap" / "Deliberately deferred" / "Documented future idea" text',
            },
        ]
        assert filter_actionable_doc_gaps(gaps, repo_dir=tmp_path) == []

    def test_proposal_already_implemented_uses_sibling_module(self, tmp_path):
        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "ledger_predictive_fast_forward.py").write_text("x = 1\n", encoding="utf-8")
        assert proposal_already_implemented(
            {"title": "Add ledger predictive fast forward"},
            tmp_path,
        )

    def test_recent_capability_titles_from_events(self):
        titles = recent_capability_titles([
            {"summary": "Opened proposal PR: Add stack-signature detector (https://x)", "details": {}},
            {"summary": "x", "details": {"title": "Wire tick-failure alerts"}},
        ])
        assert "Add stack-signature detector" in titles[0]
        assert "Wire tick-failure alerts" in titles

    def test_skips_tick_failure_classifier_when_module_present(self, tmp_path):
        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "tick_failure_classifier.py").write_text("def classify(e):\n    return {}\n", encoding="utf-8")
        gaps = [{
            "file": "docs/x.md",
            "line_no": 1,
            "anchor": "Known gap",
            "text": "Known gap: tick-failure classifier for permission-denied hints",
        }]
        assert filter_actionable_doc_gaps(gaps, repo_dir=tmp_path) == []

    def test_skips_write_guard_when_module_present(self, tmp_path):
        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "write_guard.py").write_text("def is_writable(p):\n    return True\n", encoding="utf-8")
        assert proposal_already_implemented(
            {"title": "Add a capability-scout write-guard that skips unwritable paths"},
            tmp_path,
        )
        gaps = [{
            "file": "docs/x.md",
            "line_no": 1,
            "anchor": "Known gap",
            "text": "Known gap: write-guard for unwritable allowlist paths",
        }]
        assert filter_actionable_doc_gaps(gaps, repo_dir=tmp_path) == []


# ── L4 proposal outcomes ────────────────────────────────────────────────────


class TestProposalOutcomes:
    def test_parse_reject_reason_from_label(self):
        assert parse_reject_reason(
            labels=["agentit:reject-reason:wontfix", "bug"],
            body="nope",
        ) == "wontfix"

    def test_parse_reject_reason_from_body_convention(self):
        assert parse_reject_reason(
            labels=[],
            body="Closed.\n\nagentit:reject-reason:needs-rework\n",
        ) == "needs-rework"

    def test_outcome_from_pr_status_merged(self):
        out = outcome_from_pr_status({
            "state": "merged",
            "merged_at": "2026-07-16T12:00:00Z",
            "html_url": "https://github.com/o/r/pull/20",
            "labels": [],
            "title": "Add stack-signature detector",
            "body": "",
        })
        assert out["state"] == "merged"
        assert out["pr_url"] == "https://github.com/o/r/pull/20"

    def test_outcome_from_pr_status_closed_with_wontfix(self):
        out = outcome_from_pr_status({
            "state": "closed",
            "merged_at": "",
            "html_url": "https://github.com/o/r/pull/9",
            "labels": ["agentit:reject-reason:wontfix"],
            "title": "Rewrite store",
            "body": "",
        })
        assert out["state"] == "closed"
        assert out["reject_reason"] == "wontfix"

    def test_outcome_from_pr_status_open_not_stale_returns_none(self):
        assert outcome_from_pr_status({
            "state": "open",
            "merged_at": "",
            "html_url": "https://github.com/o/r/pull/11",
            "labels": [],
            "title": "WIP",
            "body": "",
            "created_at": "2026-07-15T12:00:00Z",
        }, now="2026-07-16T12:00:00Z") is None

    def test_outcome_from_pr_status_open_stale(self):
        out = outcome_from_pr_status({
            "state": "open",
            "merged_at": "",
            "html_url": "https://github.com/o/r/pull/11",
            "labels": [],
            "title": "Stale proposal",
            "body": "",
            "created_at": "2026-06-01T12:00:00Z",
        }, now="2026-07-16T12:00:00Z")
        assert out["state"] == "stale"

    def test_filter_skips_merged_gap_titles(self, tmp_path):
        gaps = [{
            "file": "docs/x.md",
            "line_no": 1,
            "anchor": "Documented future idea",
            "text": "Documented future idea (not built): stack-signature detection logic",
        }, {
            "file": "docs/y.md",
            "line_no": 2,
            "anchor": "Known gap",
            "text": "Known gap: predictive fast-forward view on the ledger",
        }]
        outcomes = [{
            "state": "merged",
            "title": "Add stack-signature detector",
            "slug": "add-stack-signature-detector",
            "pr_url": "https://github.com/o/r/pull/20",
        }]
        kept = filter_actionable_doc_gaps(gaps, repo_dir=tmp_path, outcomes=outcomes)
        assert len(kept) == 1
        assert "fast-forward" in kept[0]["text"]

    def test_rank_prefers_untried_over_wontfix(self):
        gaps = [
            {"text": "Known gap: rewrite entire portal", "file": "a.md", "line_no": 1},
            {"text": "Known gap: predictive fast-forward view", "file": "b.md", "line_no": 2},
        ]
        outcomes = [{
            "state": "closed",
            "reject_reason": "wontfix",
            "title": "Rewrite entire portal",
            "slug": "rewrite-entire-portal",
            "recorded_at": "2026-07-10T00:00:00+00:00",
        }]
        ranked = rank_doc_gaps(gaps, outcomes)
        assert "fast-forward" in ranked[0]["text"]

    def test_proposal_blocked_by_merged_or_wontfix(self):
        outcomes = [
            {"state": "merged", "title": "Add stack-signature detector", "slug": "add-stack-signature-detector"},
            {
                "state": "closed", "reject_reason": "wontfix",
                "title": "Rewrite store layer", "slug": "rewrite-store-layer",
                "recorded_at": "2026-07-10T00:00:00+00:00",
            },
        ]
        assert proposal_blocked_by_outcome(
            {"title": "Add stack-signature detector"}, outcomes,
        )
        assert proposal_blocked_by_outcome(
            {"title": "Rewrite store layer"}, outcomes, now="2026-07-16T00:00:00+00:00",
        )
        assert not proposal_blocked_by_outcome(
            {"title": "Add ledger predictive fast forward"}, outcomes,
        )

    async def test_sync_proposal_outcomes_logs_merged_once(self):
        async_store, raw_store = await make_async_store()
        await raw_store.log_event(
            "capability-scout", CAPABILITY_RUN_ACTION, None, "info",
            "Opened proposal PR: Add stack-signature detector (https://github.com/o/r/pull/20)",
            details={
                "title": "Add stack-signature detector",
                "pr_url": "https://github.com/o/r/pull/20",
            },
        )

        def _status(url):
            return {
                "state": "merged",
                "merged_at": "2026-07-16T12:00:00Z",
                "html_url": url,
                "labels": [],
                "title": "Add stack-signature detector",
                "body": "",
                "created_at": "2026-07-15T12:00:00Z",
            }

        first = await sync_proposal_outcomes(
            async_store, get_status=_status, list_prs=lambda: [],
        )
        assert len(first) == 1
        assert first[0]["state"] == "merged"
        second = await sync_proposal_outcomes(
            async_store, get_status=_status, list_prs=lambda: [],
        )
        assert second == []
        rows = await raw_store.list_events_by_action(CAPABILITY_OUTCOME_ACTION)
        assert len(rows) == 1
        details = rows[0].get("details") or json.loads(rows[0].get("details_json") or "{}")
        assert details["pr_url"] == "https://github.com/o/r/pull/20"

    async def test_sync_proposal_outcomes_discovers_gh_only_merge(self):
        """Human/Cursor merges on self-improve branches (no capability-run pr_url)."""
        async_store, raw_store = await make_async_store()

        def _status(url):
            return {
                "state": "merged",
                "merged_at": "2026-07-16T14:24:47Z",
                "html_url": url,
                "labels": [],
                "title": "Add tick_failure_classifier for permission-denied tick-failure hints",
                "body": "",
                "created_at": "2026-07-16T14:19:55Z",
            }

        first = await sync_proposal_outcomes(
            async_store,
            get_status=_status,
            list_prs=lambda: [{
                "pr_url": "https://github.com/o/r/pull/23",
                "title": "Add tick_failure_classifier for permission-denied tick-failure hints",
            }],
        )
        assert len(first) == 1
        assert first[0]["state"] == "merged"
        assert first[0]["pr_url"] == "https://github.com/o/r/pull/23"
        second = await sync_proposal_outcomes(
            async_store,
            get_status=_status,
            list_prs=lambda: [{
                "pr_url": "https://github.com/o/r/pull/23",
                "title": "Add tick_failure_classifier for permission-denied tick-failure hints",
            }],
        )
        assert second == []

    async def test_gather_evidence_includes_outcomes_and_cites_merges(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "gap.md").write_text(
            "Known gap: predictive fast-forward view on the ledger.\n", encoding="utf-8",
        )
        async_store, raw_store = await make_async_store()
        for i in range(6):
            await raw_store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
        await raw_store.log_event(
            "capability-scout", CAPABILITY_OUTCOME_ACTION, None, "info",
            "Proposal merged: Add stack-signature detector",
            details={
                "state": "merged",
                "title": "Add stack-signature detector",
                "slug": "add-stack-signature-detector",
                "pr_url": "https://github.com/o/r/pull/20",
                "reject_reason": "",
            },
        )
        evidence = await gather_evidence(async_store, repo_dir=tmp_path)
        assert evidence["proposal_outcomes"]
        assert evidence["cited_merges"]
        assert evidence["cited_merges"][0]["pr_url"] == "https://github.com/o/r/pull/20"
        severity, _summary, details = describe_capability_run(evidence, None, None, None)
        assert details["cited_merges"]
        assert details["proposal_outcomes"]


# ── Root-cause regression: PRs #47/#53/#63/#88 (resourcequota rejection- ──
# ── sampler proposed 4x, closed 4x as duplicate/stub) ──────────────────────
#
# Real, verified root causes (see PR #63/#88's own closing comments):
# 1. get_pr_status() never reads a PR's comment thread, only body/labels --
#    every real close reason here was left as a plain comment, so
#    parse_reject_reason() never saw it and treated "closed, not merged" as
#    "still an open, remediable gap."
# 2. proposal_already_implemented() only checked a literal expected
#    filename, never whether the store already exposes the capability
#    (record_skill_outcome's `reason` column) under a different name.


class TestDuplicateRejectionDetection:
    """Fix for root cause 1: real close-comment text -> reject_reason, and
    'duplicate' treated as a permanent block (unlike wontfix's 30-day
    cooldown, which correctly assumes the underlying gap might still be
    real later)."""

    def test_parse_reject_reason_infers_duplicate_from_real_pr63_comment(self):
        # Verbatim excerpt from the real closing comment on
        # github.com/alimobrem/AgentIT/pull/63 -- no agentit:reject-reason:
        # label or body line was ever used, only this plain comment.
        real_comment = (
            "Closing \u2014 this duplicates existing, better functionality. The codebase "
            "already has a Postgres-backed rejection-tracking mechanism (skill_effectiveness "
            "table's reason column + record_skill_outcome()/get_low_effectiveness_skills()), "
            "which persists across restarts unlike this PR's in-memory RejectionSampler."
        )
        assert parse_reject_reason([], "", comments=[real_comment]) == "duplicate"

    def test_parse_reject_reason_infers_duplicate_from_real_pr88_comment(self):
        # Verbatim excerpt from github.com/alimobrem/AgentIT/pull/88.
        real_comment = (
            "Closing for the same reason as #63 (same underlying proposal, regenerated) "
            "\u2014 the capability already exists: store.py's skill_effectiveness table has "
            "a reason TEXT column..."
        )
        assert parse_reject_reason([], "", comments=[real_comment]) == "duplicate"

    def test_parse_reject_reason_explicit_prefix_still_wins_over_inference(self):
        """The exact agentit:reject-reason: convention (label/body) is
        checked before the comment-text heuristic, and still works exactly
        as before -- this fix only adds a fallback, never overrides the
        explicit signal."""
        assert parse_reject_reason(
            ["agentit:reject-reason:wontfix"], "", comments=["this duplicates something else entirely"],
        ) == "wontfix"

    def test_parse_reject_reason_no_signal_stays_empty(self):
        assert parse_reject_reason([], "just needs a rebase", comments=["looks good, will merge after CI"]) == ""

    def test_outcome_from_pr_status_picks_up_duplicate_from_comments(self):
        out = outcome_from_pr_status(
            {
                "state": "closed", "merged_at": "", "html_url": "https://github.com/o/r/pull/88",
                "labels": [], "title": "Add a skill-approval rate sampler", "body": "",
            },
            comments=["Closing for the same reason as #63 -- this duplicates existing functionality."],
        )
        assert out["state"] == "closed"
        assert out["reject_reason"] == "duplicate"

    def test_fetch_pr_close_comments_returns_real_comment_bodies(self):
        with patch(
            "agentit.portal.github_pr.fetch_pr_issue_comments",
            return_value=["Closing -- duplicates #63", "ok"],
        ) as mock_fetch:
            comments = fetch_pr_close_comments("https://github.com/o/r/pull/88")
        assert comments == ["Closing -- duplicates #63", "ok"]
        mock_fetch.assert_called_once_with("https://github.com/o/r/pull/88")

    def test_fetch_pr_close_comments_returns_empty_on_failure(self):
        with patch("agentit.portal.github_pr.fetch_pr_issue_comments", return_value=[]):
            assert fetch_pr_close_comments("https://github.com/o/r/pull/1") == []

    async def test_sync_proposal_outcomes_reads_comments_only_for_closed_prs(self):
        """The extra comments REST call must only fire for closed (not
        merged/open) PRs -- merged/open PRs have no close reason worth
        reading, so this keeps the extra API call bounded."""
        async_store, _raw = await make_async_store()
        get_comments = MagicMock(return_value=["Closing -- duplicates #63"])

        def _status(url):
            return {
                "state": "merged", "merged_at": "2026-07-16T12:00:00Z", "html_url": url,
                "labels": [], "title": "Merged thing", "body": "", "created_at": "2026-07-15T12:00:00Z",
            }

        await sync_proposal_outcomes(
            async_store, get_status=_status, list_prs=lambda: [{"pr_url": "https://x/pull/1", "title": "Merged thing"}],
            get_comments=get_comments,
        )
        get_comments.assert_not_called()

    async def test_sync_proposal_outcomes_records_duplicate_reject_reason_from_comments(self):
        """End-to-end: a closed PR whose only reason signal is a real
        free-form comment (the actual PR #88 shape) is recorded with
        reject_reason='duplicate', not blank."""
        async_store, raw_store = await make_async_store()

        def _status(url):
            return {
                "state": "closed", "merged_at": "", "html_url": url, "labels": [],
                "title": "Add a skill-approval rate sampler that records structured per-rejection context",
                "body": "", "created_at": "2026-07-17T14:00:00Z",
            }

        def _comments(url):
            return ["Closing for the same reason as #63 -- this duplicates existing, better functionality."]

        newly = await sync_proposal_outcomes(
            async_store, get_status=_status, get_comments=_comments,
            list_prs=lambda: [{"pr_url": "https://github.com/o/r/pull/88", "title": "irrelevant"}],
        )
        assert len(newly) == 1
        assert newly[0]["reject_reason"] == "duplicate"
        rows = await raw_store.list_events_by_action(CAPABILITY_OUTCOME_ACTION)
        details = rows[0].get("details") or json.loads(rows[0].get("details_json") or "{}")
        assert details["reject_reason"] == "duplicate"

    def test_proposal_blocked_by_duplicate_outcome_has_no_cooldown(self):
        """Unlike wontfix (30-day cooldown), a duplicate outcome blocks
        forever -- an already-existing capability doesn't stop existing
        after 30 days. Recorded 200 days ago, still blocks."""
        outcomes = [{
            "state": "closed", "reject_reason": "duplicate",
            "title": "Add a resourcequota skill rejection sampler that records structured "
                     "rejection reasons for zero-approval skills",
            "slug": "add-a-resourcequota-skill-rejection-sampler",
            "recorded_at": "2025-12-30T00:00:00+00:00",
        }]
        assert proposal_blocked_by_outcome(
            {
                "title": "Add a skill-approval rate sampler that records structured "
                         "per-rejection context for low-effectiveness skills",
            },
            outcomes,
            now="2026-07-17T00:00:00+00:00",
        )

    def test_rank_doc_gaps_deprioritizes_duplicate_same_as_wontfix(self):
        gaps = [
            {"text": "Known gap: rewrite entire portal", "file": "a.md", "line_no": 1},
            {"text": "Known gap: predictive fast-forward view", "file": "b.md", "line_no": 2},
        ]
        outcomes = [{
            "state": "closed", "reject_reason": "duplicate",
            "title": "Rewrite entire portal", "slug": "rewrite-entire-portal",
            "recorded_at": "2026-07-10T00:00:00+00:00",
        }]
        ranked = rank_doc_gaps(gaps, outcomes)
        assert "fast-forward" in ranked[0]["text"]

    def test_filter_actionable_doc_gaps_drops_duplicate_titles_like_wontfix(self, tmp_path):
        gaps = [{
            "file": "docs/x.md", "line_no": 1, "anchor": "Known gap",
            "text": "Known gap: add a resourcequota skill rejection sampler",
        }]
        outcomes = [{
            "state": "closed", "reject_reason": "duplicate",
            "title": "Add a resourcequota skill rejection sampler",
            "slug": "add-a-resourcequota-skill-rejection-sampler",
        }]
        assert filter_actionable_doc_gaps(gaps, repo_dir=tmp_path, outcomes=outcomes) == []


class TestStoreCapabilityEvidence:
    """Fix for root cause 2: real evidence about what the store already
    does, so a proposal can be checked against actual capabilities, not
    just an expected filename."""

    async def test_list_store_capabilities_includes_real_methods(self):
        async_store, _raw = await make_async_store()
        caps = list_store_capabilities(async_store)
        assert "record_skill_outcome" in caps
        assert "get_low_effectiveness_skills" in caps
        assert "get_recent_skill_activity" in caps
        # Private/dunder methods never leak into LLM-facing evidence.
        assert not any(c.startswith("_") for c in caps)

    def test_list_store_capabilities_empty_for_no_store(self):
        assert list_store_capabilities(None) == []

    def test_proposal_already_implemented_flags_rejection_sampler_when_capability_confirmed(self):
        """The exact PR #63/#88 title+gap_description shape, checked
        against a real (confirmed) store_capabilities list -- must be
        flagged as already-implemented instead of gated only on a literal
        filename match."""
        proposal = {
            "title": "Add a skill-approval rate sampler that records structured "
                     "per-rejection context for low-effectiveness skills",
            "gap_description": (
                "The resourcequota skill has a weighted approval rate of only 16.7% with no "
                "structured per-rejection context being captured. A new small module can "
                "record rejection reasons per skill tick."
            ),
        }
        assert proposal_already_implemented(
            proposal, Path("/nonexistent"), store_capabilities=["record_skill_outcome", "get_agent_stats"],
        )

    def test_proposal_already_implemented_does_not_guess_without_confirmed_capability(self):
        """No store_capabilities evidence available this cycle (e.g. store
        is None) -> this check makes no claim, rather than assuming the
        method exists. Sibling-module/shipped-module checks are unaffected."""
        proposal = {
            "title": "Add a skill-approval rate sampler that records structured "
                     "per-rejection context for low-effectiveness skills",
            "gap_description": "records rejection reasons per skill tick",
        }
        assert not proposal_already_implemented(proposal, Path("/nonexistent"), store_capabilities=[])
        assert not proposal_already_implemented(proposal, Path("/nonexistent"), store_capabilities=None)

    def test_proposal_already_implemented_ignores_unrelated_titles(self):
        """The capability-phrase check is narrow -- an unrelated proposal
        title/gap must never be flagged just because record_skill_outcome
        happens to be in store_capabilities."""
        proposal = {"title": "Add a retry backoff to the drift detector", "gap_description": "retries too fast"}
        assert not proposal_already_implemented(
            proposal, Path("/nonexistent"), store_capabilities=["record_skill_outcome"],
        )

    async def test_gather_evidence_includes_store_capabilities_and_recent_skill_activity(self, monkeypatch, tmp_path):
        """Proves the LLM now actually receives evidence that
        record_skill_outcome (with a real 'reason') already exists --
        the concrete piece of evidence missing from every one of the four
        real proposals that re-invented it."""
        monkeypatch.chdir(tmp_path)
        async_store, raw_store = await make_async_store()
        for _ in range(5):
            await raw_store.record_skill_outcome("resourcequota", "pinky", "rejected", "quota exceeded")
        await raw_store.record_skill_outcome("resourcequota", "pinky", "approved", "")

        evidence = await gather_evidence(async_store, repo_dir=tmp_path)

        assert "record_skill_outcome" in evidence["store_capabilities"]
        assert "get_low_effectiveness_skills" in evidence["store_capabilities"]
        assert evidence["recent_skill_activity"]
        assert any(row.get("reason") == "quota exceeded" for row in evidence["recent_skill_activity"])
        # Not counted as "real signal that justifies a cycle" on its own.
        assert evidence["signal_count"] >= len(evidence["low_effectiveness_skills"])

    async def test_gather_evidence_store_capabilities_empty_without_a_store(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        evidence = await gather_evidence(None)
        assert evidence["store_capabilities"] == []
        assert evidence["recent_skill_activity"] == []


class TestResourcequotaRejectionSamplerRegression:
    """Full simulation of the real, twice(-plus)-repeated bug: a
    'resourcequota' skill-effectiveness gap that's already covered by
    skill_effectiveness/record_skill_outcome, with a prior PR on the same
    topic closed as a duplicate via a plain comment (no label, no
    agentit:reject-reason: body line -- the real observed shape). Confirms
    gather_evidence + the block/already-implemented checks now stop a
    second near-identical proposal, using real store data throughout (no
    mock data for the actual skill_effectiveness rows)."""

    async def _seeded_evidence(self, tmp_path, raw_store, async_store):
        for _ in range(5):
            await raw_store.record_skill_outcome("resourcequota", "pinky", "rejected", "quota exceeded")
        await raw_store.record_skill_outcome("resourcequota", "pinky", "approved", "")
        # Real fleet-wide rejection signal, matching MIN_SIGNAL_ROWS's bar
        # for "enough real data to ground a proposal at all" -- unrelated to
        # the specific resourcequota/duplicate mechanics under test here.
        for i in range(4):
            await raw_store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
        # The real PR #63 outcome: closed as a duplicate, discovered only
        # via its plain closing comment (sync_proposal_outcomes already
        # covered elsewhere) -- recorded directly here as the
        # capability-outcome event gather_evidence() reads back.
        await raw_store.log_event(
            "capability-scout", CAPABILITY_OUTCOME_ACTION, None, "info",
            "Proposal closed: Add a resourcequota skill rejection sampler that records "
            "structured rejection reasons for zero-approval skills (duplicate)",
            details={
                "state": "closed",
                "title": "Add a resourcequota skill rejection sampler that records structured "
                         "rejection reasons for zero-approval skills",
                "slug": "add-a-resourcequota-skill-rejection-sampler-that-records-structu",
                "pr_url": "https://github.com/alimobrem/AgentIT/pull/63",
                "reject_reason": "duplicate",
                "recorded_at": "2026-07-17T00:46:23Z",
            },
        )
        return await gather_evidence(async_store, repo_dir=tmp_path)

    async def test_evidence_proves_the_capability_already_exists(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        async_store, raw_store = await make_async_store()
        evidence = await self._seeded_evidence(tmp_path, raw_store, async_store)

        assert any(s.get("skill") == "resourcequota" for s in evidence["low_effectiveness_skills"])
        assert "record_skill_outcome" in evidence["store_capabilities"]
        assert any(row.get("reason") for row in evidence["recent_skill_activity"])
        assert evidence["proposal_outcomes"]
        assert evidence["proposal_outcomes"][0]["reject_reason"] == "duplicate"

    async def test_a_fresh_near_identical_proposal_is_blocked_by_outcome(self, monkeypatch, tmp_path):
        """A new LLM proposal re-describing the exact same capability under
        a new title (the real PR #88 shape, regenerated weeks after #63)
        must be blocked before it ever reaches the safety gates / opens a
        second PR."""
        monkeypatch.chdir(tmp_path)
        async_store, raw_store = await make_async_store()
        evidence = await self._seeded_evidence(tmp_path, raw_store, async_store)

        new_proposal = {
            "has_proposal": True,
            "title": "Add a skill-approval rate sampler that records structured "
                     "per-rejection context for low-effectiveness skills",
            "gap_description": (
                "The resourcequota skill has a weighted approval rate of only 16.7% with no "
                "structured per-rejection context being captured."
            ),
            "target_files": ["src/agentit/skill_rejection_sampler.py", "tests/test_skill_rejection_sampler.py"],
            "risk": "low",
            "test_plan": "Assert record_rejection persists structured samples.",
        }
        assert proposal_blocked_by_outcome(new_proposal, evidence["proposal_outcomes"])
        assert proposal_already_implemented(
            new_proposal, tmp_path, store_capabilities=evidence["store_capabilities"],
        )

    async def test_research_once_skips_the_duplicate_instead_of_opening_a_second_pr(self, monkeypatch, tmp_path):
        """Full watcher-level proof: research_once() must not open a PR for
        the regenerated duplicate. It stops at the already-implemented
        check (store_capabilities confirms record_skill_outcome already
        covers this) before even reaching the outcome-blocked check --
        either is a correct fix; already-implemented is the more precise
        diagnosis, and test_a_fresh_near_identical_proposal_is_blocked_by_outcome
        above separately proves the outcome-blocked path also fires for
        this exact case."""
        from unittest.mock import MagicMock as _MM

        from agentit.watchers.capability_scout import CapabilityScout

        monkeypatch.chdir(tmp_path)
        async_store, raw_store = await make_async_store()
        await self._seeded_evidence(tmp_path, raw_store, async_store)

        publisher = _MM()
        scout = CapabilityScout(publisher=publisher, store=async_store, repo_dir=tmp_path)

        mock_llm = _MM()
        mock_llm.propose_capability_improvement.return_value = {
            "has_proposal": True,
            "title": "Add a skill-approval rate sampler that records structured "
                     "per-rejection context for low-effectiveness skills",
            "gap_description": "records rejection reasons per skill tick",
            "target_files": ["src/agentit/skill_rejection_sampler.py", "tests/test_skill_rejection_sampler.py"],
            "risk": "low",
            "test_plan": "Assert record_rejection persists structured samples.",
        }
        with patch("agentit.llm.LLMClient", return_value=mock_llm), \
             patch("agentit.git_pr.create_branch_commit_push") as mock_branch, \
             patch("agentit.git_pr.open_draft_pr") as mock_open_pr:
            result = await scout.research_once()

        assert result["outcome"] in ("already-implemented", "outcome-blocked")
        mock_branch.assert_not_called()
        mock_open_pr.assert_not_called()
        publisher.publish.assert_not_called()


# ── gather_evidence ─────────────────────────────────────────────────────────


class TestGatherEvidence:
    async def test_aggregates_real_store_signal(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        async_store, raw_store = await make_async_store()
        await raw_store.record_feedback("app-a", "hardening", "network-policy", "rejected")
        await raw_store.record_feedback("app-b", "hardening", "network-policy", "rejected")

        evidence = await gather_evidence(async_store)

        assert evidence["rejection_stats"]
        assert evidence["signal_count"] >= 1

    async def test_no_store_still_scans_docs_but_no_signal(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        evidence = await gather_evidence(None)
        assert evidence["signal_count"] == 0
        assert evidence["doc_gaps"] == []

    async def test_store_query_failure_does_not_raise(self, monkeypatch, tmp_path):
        """A missing/failing store method must never block the rest of
        evidence-gathering -- mirrors every other hasattr()-guarded store
        call already used throughout this repo."""
        monkeypatch.chdir(tmp_path)

        class _BrokenStore:
            async def get_fleet_wide_rejection_stats(self):
                raise RuntimeError("db exploded")

        evidence = await gather_evidence(_BrokenStore())
        assert evidence["rejection_stats"] == []
        assert evidence["signal_count"] == 0


# ── build_diff / render_proposal_doc ────────────────────────────────────────


class TestBuildDiff:
    def test_produces_one_doc_file_under_docs_proposals(self):
        diff = build_diff(_proposal(title="Add a widget"))
        assert len(diff) == 1
        path = next(iter(diff))
        assert path == "docs/proposals/add-a-widget.md"
        assert "Add a widget" in diff[path]

    def test_render_includes_every_proposal_field_verbatim(self):
        content = render_proposal_doc(_proposal())
        assert "README.md:42" in content
        assert "Add a counter and a threshold check" in content
        assert "Assert the threshold logic in a new test" in content
        assert "src/agentit/portal/store.py" in content

    def test_slugify_handles_punctuation(self):
        assert slugify("Fix: the [Foo] Bar!!") == "fix-the-foo-bar"
        assert slugify("") == "proposal"


# ── source / auto mode (L3 dogfood) ─────────────────────────────────────────


class TestResolveBuildMode:
    def test_docs_mode_always_docs(self):
        from agentit.capability_scout import resolve_build_mode

        assert resolve_build_mode(_proposal(target_files=["skills/security/x.md"]), "docs") == "docs"

    def test_auto_picks_source_for_skills_checks_tests_only(self):
        from agentit.capability_scout import resolve_build_mode

        assert resolve_build_mode(
            _proposal(target_files=["skills/security/x.md", "tests/test_x.py"]), "auto",
        ) == "source"
        assert resolve_build_mode(
            _proposal(target_files=["src/agentit/portal/store.py", "tests/test_store.py"]), "auto",
        ) == "source"
        assert resolve_build_mode(
            _proposal(target_files=["docs/proposals/x.md"]), "auto",
        ) == "docs"

    def test_source_mode_falls_back_to_docs_when_targets_ineligible(self):
        from agentit.capability_scout import resolve_build_mode

        assert resolve_build_mode(
            _proposal(target_files=["src/agentit/llm.py", "chart/values.yaml"]), "source",
        ) == "docs"


class TestRewriteOversizedSourceTargets:
    def test_keeps_small_and_new_files(self, tmp_path):
        from agentit.capability_scout import rewrite_oversized_source_targets

        skill = tmp_path / "skills" / "security"
        skill.mkdir(parents=True)
        (skill / "small.md").write_text("tiny\n", encoding="utf-8")
        out = rewrite_oversized_source_targets(
            ["skills/security/small.md", "tests/test_new.py"],
            tmp_path,
            title="Add widget",
        )
        assert out == ["skills/security/small.md", "tests/test_new.py"]

    def test_replaces_oversized_existing_with_sibling(self, tmp_path):
        from agentit.capability_scout import (
            MAX_DIFF_LINES,
            rewrite_oversized_source_targets,
            sibling_module_path,
        )

        big = tmp_path / "src" / "agentit"
        big.mkdir(parents=True)
        (big / "capability_scout.py").write_text(
            "\n".join(f"line_{i}" for i in range(MAX_DIFF_LINES + 20)) + "\n",
            encoding="utf-8",
        )
        out = rewrite_oversized_source_targets(
            ["src/agentit/capability_scout.py", "tests/test_stack_signature_detection.py"],
            tmp_path,
            title="Add cross-onboarding stack-signature detector",
        )
        sibling = sibling_module_path("Add cross-onboarding stack-signature detector")
        assert sibling in out
        assert "src/agentit/capability_scout.py" not in out
        assert "tests/test_stack_signature_detection.py" in out

    def test_build_diff_rewrites_oversized_before_llm(self, tmp_path):
        from agentit.capability_scout import MAX_DIFF_LINES, build_diff, sibling_module_path

        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "capability_scout.py").write_text(
            "\n".join(f"x{i}" for i in range(MAX_DIFF_LINES + 5)) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        sibling = sibling_module_path("Add stack signature detector")
        llm = MagicMock()
        llm.generate_capability_files.return_value = {
            sibling: "def detect():\n    return []\n",
            "tests/test_stack_signature_detection.py": "def test_detect():\n    assert True\n",
        }
        proposal = _proposal(
            title="Add stack signature detector",
            target_files=[
                "src/agentit/capability_scout.py",
                "tests/test_stack_signature_detection.py",
            ],
        )
        diff = build_diff(proposal, mode="auto", repo_dir=tmp_path, llm_client=llm)
        assert set(diff) == {sibling, "tests/test_stack_signature_detection.py"}
        # LLM was asked for the rewritten targets, not the oversized module.
        called_targets = llm.generate_capability_files.call_args[0][1]
        assert "src/agentit/capability_scout.py" not in called_targets
        assert sibling in called_targets


class TestBuildSourceDiff:
    def test_build_diff_source_mode_uses_llm_file_map(self, tmp_path):
        from agentit.capability_scout import build_diff

        skill = tmp_path / "skills" / "security"
        skill.mkdir(parents=True)
        (skill / "existing.md").write_text("---\nname: existing\n---\nold\n", encoding="utf-8")

        llm = MagicMock()
        llm.generate_capability_files.return_value = {
            "skills/security/existing.md": "---\nname: existing\n---\nnew body\n",
        }
        proposal = _proposal(
            title="Improve existing skill",
            target_files=["skills/security/existing.md"],
        )
        diff = build_diff(proposal, mode="source", repo_dir=tmp_path, llm_client=llm)

        assert diff == {
            "skills/security/existing.md": "---\nname: existing\n---\nnew body\n",
        }
        llm.generate_capability_files.assert_called_once()
        call = llm.generate_capability_files.call_args
        current = call.args[1] if call.args and len(call.args) > 1 else call.kwargs.get("current_files")
        assert "skills/security/existing.md" in current
        assert "old" in current["skills/security/existing.md"]

    def test_build_diff_source_mode_allows_src_agentit(self, tmp_path):
        from agentit.capability_scout import build_diff

        src = tmp_path / "src" / "agentit"
        src.mkdir(parents=True)
        (src / "widget.py").write_text("def old():\n    return 0\n", encoding="utf-8")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_widget.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")

        llm = MagicMock()
        llm.generate_capability_files.return_value = {
            "src/agentit/widget.py": "def new():\n    return 1\n",
            "tests/test_widget.py": "def test_new():\n    assert True\n",
        }
        proposal = _proposal(
            title="Improve widget",
            target_files=["src/agentit/widget.py", "tests/test_widget.py"],
        )
        diff = build_diff(proposal, mode="auto", repo_dir=tmp_path, llm_client=llm)
        assert set(diff) == {"src/agentit/widget.py", "tests/test_widget.py"}
        assert "def new()" in diff["src/agentit/widget.py"]

    def test_build_diff_source_rejects_llm_paths_outside_targets(self, tmp_path):
        from agentit.capability_scout import build_diff

        llm = MagicMock()
        llm.generate_capability_files.return_value = {
            "skills/security/ok.md": "ok",
            "src/agentit/evil.py": "nope",
        }
        proposal = _proposal(target_files=["skills/security/ok.md"])
        diff = build_diff(proposal, mode="source", repo_dir=tmp_path, llm_client=llm)
        assert diff == {"skills/security/ok.md": "ok"}

    def test_build_diff_source_returns_empty_when_llm_returns_nothing(self, tmp_path):
        """Source/auto must not open a docs-only PR when generation fails —
        that burned max-open-prs with fake Build mode: source drafts."""
        from agentit.capability_scout import build_diff

        llm = MagicMock()
        llm.generate_capability_files.return_value = None
        proposal = _proposal(title="Fallback", target_files=["skills/security/x.md"])
        diff = build_diff(proposal, mode="source", repo_dir=tmp_path, llm_client=llm)
        assert diff == {}
        diff_auto = build_diff(proposal, mode="auto", repo_dir=tmp_path, llm_client=llm)
        assert diff_auto == {}

    def test_build_diff_docs_mode_still_writes_proposal_md(self, tmp_path):
        from agentit.capability_scout import build_diff

        proposal = _proposal(title="Docs only", target_files=["skills/security/x.md"])
        diff = build_diff(proposal, mode="docs", repo_dir=tmp_path, llm_client=MagicMock())
        assert list(diff) == ["docs/proposals/docs-only.md"]


# ── Safety gates ────────────────────────────────────────────────────────────


class TestCheckDiffSize:
    def test_passes_under_cap(self):
        passed, _detail = check_diff_size({"docs/proposals/a.md": "line1\nline2"})
        assert passed is True

    def test_fails_over_file_count_cap(self):
        diff = {f"docs/proposals/{i}.md": "x" for i in range(MAX_DIFF_FILES + 1)}
        passed, detail = check_diff_size(diff)
        assert passed is False
        assert "file" in detail

    def test_fails_over_line_count_cap(self):
        diff = {"docs/proposals/a.md": "\n".join(["line"] * (MAX_DIFF_LINES + 10))}
        passed, detail = check_diff_size(diff)
        assert passed is False
        assert "line" in detail


class TestCheckScopeAllowlist:
    def test_passes_for_docs_path(self):
        passed, _detail = check_scope_allowlist({"docs/proposals/a.md": "x"})
        assert passed is True

    def test_passes_for_src_agentit_path(self):
        passed, _detail = check_scope_allowlist({"src/agentit/foo.py": "x"})
        assert passed is True

    def test_fails_for_chart_path(self):
        passed, detail = check_scope_allowlist({"chart/templates/foo.yaml": "x"})
        assert passed is False
        assert "chart" in detail.lower() or "scope" in detail.lower()

    def test_fails_for_path_with_secret_substring(self):
        passed, _detail = check_scope_allowlist({"src/agentit/secret_stuff.py": "x"})
        assert passed is False

    def test_fails_for_argocd_path(self):
        passed, _detail = check_scope_allowlist({"argocd/application.yaml": "x"})
        assert passed is False


class TestCheckNoSecrets:
    def test_passes_for_clean_content(self):
        passed, _detail = check_no_secrets({"docs/proposals/a.md": "nothing sensitive here"})
        assert passed is True

    def test_fails_for_aws_key_pattern(self):
        passed, detail = check_no_secrets({"docs/proposals/a.md": "key = AKIAIOSFODNN7EXAMPLE"})
        assert passed is False
        assert "secret" in detail.lower()

    def test_fails_for_private_key_pattern(self):
        passed, _detail = check_no_secrets({"a.py": "-----BEGIN RSA PRIVATE KEY-----"})
        assert passed is False


class TestCheckHasTestPlan:
    def test_passes_when_test_plan_present(self):
        passed, _detail = check_has_test_plan(_proposal(test_plan="Assert X happens"))
        assert passed is True

    def test_fails_when_test_plan_empty(self):
        passed, detail = check_has_test_plan(_proposal(test_plan=""))
        assert passed is False
        assert "test_plan" in detail or "test" in detail.lower()


class TestCheckEvidenceUsefulness:
    def test_passes_when_dogfood_cited(self):
        passed, _detail = check_evidence_usefulness(_proposal())
        assert passed is True

    def test_fails_without_concrete_failure_signal(self):
        passed, detail = check_evidence_usefulness(_proposal(
            evidence="We should maybe improve logging someday for operators.",
            gap_description="Logging could be nicer in general.",
            change_summary="Add a log line",
            title="Nicer logs",
        ))
        assert passed is False
        assert "concrete" in detail.lower() or "dogfood" in detail.lower()


class TestCheckSyntax:
    def test_passes_for_valid_python(self):
        passed, _detail = check_syntax({"src/agentit/foo.py": "def foo():\n    return 1\n"})
        assert passed is True

    def test_fails_for_invalid_python(self):
        passed, detail = check_syntax({"src/agentit/foo.py": "def foo(:\n    return 1\n"})
        assert passed is False
        assert "foo.py" in detail

    def test_skips_non_python_files(self):
        passed, _detail = check_syntax({"docs/proposals/a.md": "not python at all {{{"})
        assert passed is True


class TestLastMergeBrokeCi:
    def test_true_when_real_tests_pass_failure_follows_merge(self):
        outcomes = [{
            "state": "merged",
            "recorded_at": "2026-07-01T00:00:00+00:00",
            "title": "shipped something",
        }]
        events = [{
            "timestamp": "2026-07-01T01:00:00+00:00",
            "details": {
                "gate_results": [
                    {"name": "tests-pass", "passed": False, "detail": "1 failed"},
                ],
            },
        }]
        assert last_merge_broke_ci(outcomes, events) is True

    def test_false_when_only_all_skip_infra_failure(self):
        """All-skip (missing AGENTIT_TEST_PG_DSN) must not stick
        fix_regression_only — that locked dogfood into no-signal declines."""
        outcomes = [{
            "state": "merged",
            "recorded_at": "2026-07-01T00:00:00+00:00",
            "title": "shipped something",
        }]
        events = [{
            "timestamp": "2026-07-01T01:00:00+00:00",
            "details": {
                "gate_results": [{
                    "name": "tests-pass",
                    "passed": False,
                    "detail": (
                        "pytest exited 0 but 0 tests actually passed -- the whole "
                        "suite skipped itself (likely no Postgres reachable in this "
                        "pod; see run_test_suite()'s docstring): "
                        "no AGENTIT_TEST_PG_DSN and no podman/docker on PATH"
                    ),
                }],
            },
        }]
        assert last_merge_broke_ci(outcomes, events) is False


class TestRunTestSuite:
    def test_returns_true_when_pytest_exits_zero(self, tmp_path):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="12 passed in 1.23s", stderr="",
            )
            passed, detail = run_test_suite(tmp_path)
        assert passed is True
        assert "passed" in detail

    def test_returns_false_when_pytest_exits_zero_but_every_test_skipped(self, tmp_path):
        """Real regression test: without AGENTIT_TEST_PG_DSN (and with no
        podman/docker in the image) every test's session-scoped
        `postgres_dsn` fixture calls `pytest.skip(...)` and the suite
        exits 0 with nothing actually run. Confirmed live before the
        chart wired a throwaway test-postgres sidecar. Before the
        all-skip check, `run_test_suite()` read that as "pytest passed"
        and this fail-closed gate would wave a proposal through having
        verified precisely nothing. The gate must still fail closed on
        all-skip even after the sidecar lands (sidecar crash / miswire)."""
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="ssssssssssssssssssssssssssssssssssssssssssssss [100%]\n87 skipped in 0.04s\n",
                stderr="",
            )
            passed, detail = run_test_suite(tmp_path)
        assert passed is False
        assert "0 tests actually passed" in detail

    def test_all_skipped_failure_detail_surfaces_the_skip_reason(self, tmp_path):
        """The skip-reason detail (e.g. "no AGENTIT_TEST_PG_DSN and no
        podman/docker on PATH to start one") only appears in pytest's
        captured stdout with `-rs` passed -- assert the gate actually asks
        for it and surfaces it, since a bare "0 tests actually passed" with
        no reason would be as undiagnosable as the bug this replaces."""
        skip_reason = "no AGENTIT_TEST_PG_DSN and no podman/docker on PATH to start one"
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"SKIPPED [87] tests/test_capability_scout.py:900: {skip_reason}\n"
                       "87 skipped in 0.04s\n",
                stderr="",
            )
            passed, detail = run_test_suite(tmp_path)
        assert passed is False
        assert skip_reason in detail

    def test_invokes_pytest_with_skip_reason_reporting_enabled(self, tmp_path):
        """`-rs` is what makes skip reasons show up in stdout at all --
        without it the all-skipped detection above would have nothing
        diagnosable to surface."""
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1 passed in 0.01s", stderr="")
            run_test_suite(tmp_path)
        args, _kwargs = mock_run.call_args
        assert "-rs" in args[0]

    def test_returns_false_when_pytest_exits_nonzero(self, tmp_path):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="1 failed", stderr="")
            passed, detail = run_test_suite(tmp_path)
        assert passed is False
        assert "1" in detail

    def test_returns_false_on_timeout_without_raising(self, tmp_path):
        with patch(
            "agentit.capability_scout.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=900),
        ):
            passed, _detail = run_test_suite(tmp_path)
        assert passed is False

    def test_invokes_with_the_exact_ci_flags_and_kubeconfig_env(self, tmp_path):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_test_suite(tmp_path)
        args, kwargs = mock_run.call_args
        command = args[0]
        assert "--ignore=tests/test_real_repos.py" in command
        assert "--ignore=tests/test_browser.py" in command
        assert "--ignore=tests/test_browser_critical.py" in command
        assert "--ignore=tests/test_live_cluster_e2e.py" in command
        assert kwargs["env"]["KUBECONFIG"] == "/tmp/nonexistent-path"

    def test_failure_detail_surfaces_stderr_when_stdout_is_empty(self, tmp_path):
        """Real regression test for the production `tests-pass` gate bug:
        the capability-scout image never installed the 'dev' extra (pytest)
        nor shipped tests/, so every real cycle's pytest invocation died
        immediately with "No module named pytest" on stderr while stdout
        stayed empty. run_test_suite() used to build its failure detail from
        stdout alone, so the gate's recorded event showed the completely
        uninformative "pytest exited 1: " -- this is what actually blocked
        debugging live. Confirmed against the real agentit-capability-scout
        pod: `python -m pytest tests/ ...` there exits 1 with stdout=""
        and stderr="/opt/app-root/bin/python: No module named pytest"."""
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="",
                stderr="/opt/app-root/bin/python: No module named pytest\n",
            )
            passed, detail = run_test_suite(tmp_path)
        assert passed is False
        assert "No module named pytest" in detail

    def test_strips_in_cluster_service_account_env_vars(self, tmp_path, monkeypatch):
        """Regression test for the dominant real root cause of the live
        `tests-pass` gate hanging for ~900s+ and getting killed (confirmed
        twice, at two different resource ceilings): overriding KUBECONFIG
        alone does nothing when this gate runs inside a real cluster pod,
        because kube.py's config loader tries load_incluster_config()
        FIRST, which only checks KUBERNETES_SERVICE_HOST/PORT and the
        mounted service-account token -- never KUBECONFIG. Confirmed live
        against the real agentit-capability-scout pod: load_incluster_config()
        succeeded in <1ms with KUBECONFIG overridden, only failing (and
        falling back to the fast, KUBECONFIG-respecting path) once these
        two vars were also stripped."""
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_test_suite(tmp_path)
        _args, kwargs = mock_run.call_args
        assert "KUBERNETES_SERVICE_HOST" not in kwargs["env"]
        assert "KUBERNETES_SERVICE_PORT" not in kwargs["env"]
        assert kwargs["env"]["KUBECONFIG"] == "/tmp/nonexistent-path"

    def test_strips_ambient_postgres_backend_env_vars(self, tmp_path, monkeypatch):
        """Regression test: store_factory.create_store() reads
        AGENTIT_DB_BACKEND directly, so leaving it set to 'postgres' (as
        this watcher's own Deployment does, to share the real fleet's
        store) makes any test that doesn't explicitly monkeypatch it
        silently connect to the *real* shared Postgres instead of its own
        isolated fixture -- confirmed live: test_watch_rescan_iterates_the_fleet
        saw the real fleet's actual apps instead of its one fixture app."""
        monkeypatch.setenv("AGENTIT_DB_BACKEND", "postgres")
        monkeypatch.setenv("AGENTIT_DB_DSN", "postgresql://real-prod-host/agentit")
        monkeypatch.setenv("PGUSER", "realuser")
        monkeypatch.setenv("PGPASSWORD", "realpass")
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_test_suite(tmp_path)
        _args, kwargs = mock_run.call_args
        for var in ("AGENTIT_DB_BACKEND", "AGENTIT_DB_DSN", "PGUSER", "PGPASSWORD"):
            assert var not in kwargs["env"], f"{var} must be stripped so tests default to sqlite"

    def test_real_subprocess_reports_missing_tests_dir_via_stderr(self, tmp_path):
        """Unmocked end-to-end run against a real sandbox with no tests/
        directory -- reproduces the second real production bug (the image
        never `COPY tests/ tests/`), verified against a real Python
        interpreter/pytest, not a mocked subprocess. Every other test in
        this class patches subprocess.run, which would have hidden this
        exact class of infra bug indefinitely -- this one actually shells
        out, the same way the live gate does."""
        passed, detail = run_test_suite(tmp_path)
        assert passed is False
        assert "not found" in detail.lower()
        assert "tests" in detail.lower()


class TestContainerfileShipsWhatTestsPassGateNeeds:
    """Regression test for the root cause of every real capability-scout
    cycle's tests-pass gate failing with an opaque "pytest exited 1": the
    Containerfile installed only runtime deps (pip install .) and never
    copied tests/, so run_test_suite()'s `python -m pytest tests/ ...`
    subprocess -- run against repo_dir=Path.cwd(), the running container's
    own filesystem, per watchers/capability_scout.py -- had neither pytest
    importable nor a tests/ directory to point at. Confirmed live against
    the real agentit-capability-scout pod (`No module named pytest`,
    exit 1). Without this check, either half of the fix could silently
    regress on a future Containerfile edit."""

    def _containerfile_text(self) -> str:
        containerfile = Path(__file__).resolve().parent.parent / "Containerfile"
        return containerfile.read_text(encoding="utf-8")

    def test_installs_the_dev_extra_so_pytest_is_importable(self):
        content = self._containerfile_text()
        assert "[dev]" in content, (
            "Containerfile must pip install the 'dev' extra (pytest, "
            "pytest-asyncio, httpx) or capability_scout.py's tests-pass "
            "gate always fails with 'No module named pytest'"
        )

    def test_copies_the_tests_directory_into_the_image(self):
        content = self._containerfile_text()
        assert "COPY tests/ tests/" in content, (
            "Containerfile must COPY tests/ tests/ or capability_scout.py's "
            "tests-pass gate always fails with "
            "'ERROR: file or directory not found: tests/'"
        )

    def test_copies_the_chart_directory_into_the_image(self):
        """Regression test: most of tests/test_helm_templates.py (plus a
        chart-consistency check in test_helpers.py) reads
        chart/templates/*.yaml straight off disk relative to the repo
        root. Without shipping chart/, every one of those tests fails with
        a bare FileNotFoundError inside the real tests-pass gate,
        regardless of the actual proposal under test -- confirmed live."""
        content = self._containerfile_text()
        assert "COPY chart/ chart/" in content, (
            "Containerfile must COPY chart/ chart/ or every "
            "test_helm_templates.py test fails with FileNotFoundError "
            "inside the tests-pass gate"
        )

    def test_copies_the_docs_directory_into_the_image(self):
        """Regression test: scan_doc_gaps() defaults to Path("docs") under
        WORKDIR. Without shipping docs/, is_dir() is False and doc_gaps is
        always [] -- capability-scout loses its highest-precision signal
        every cycle."""
        content = self._containerfile_text()
        assert "COPY docs/ docs/" in content, (
            "Containerfile must COPY docs/ docs/ or capability_scout."
            "scan_doc_gaps() always returns [] in the running image"
        )

    def test_makes_git_directories_group_writable(self):
        """Regression test: COPY .git ./.git lands owned by root with mode
        755 (group has read+execute but not write), which blocks git from
        creating new lock files/refs inside it under OpenShift's arbitrary,
        non-root, gid-0 runtime UID. Confirmed live: a real capability-scout
        PR attempt failed with "Unable to create '.git/HEAD.lock':
        Permission denied" -- the tests-pass gate had passed, but the PR
        this whole loop exists to open still couldn't be created."""
        content = self._containerfile_text()
        assert "chmod g+w" in content and ".git" in content, (
            "Containerfile must chmod g+w the copied .git directories or "
            "every real PR attempt fails with 'Unable to create "
            "'.git/HEAD.lock': Permission denied'"
        )

    def test_makes_source_allowlist_directories_group_writable(self):
        """L3 source mode writes new files under tests/skills/checks/src/docs
        before opening a PR. Those COPY dirs also land root-owned 755; without
        g+w, scout fails at write_text with PermissionError after gates pass
        (confirmed live: tests/test_stack_signature_detector.py)."""
        content = self._containerfile_text()
        for path in ("tests", "skills", "checks", "src", "docs"):
            assert path in content, f"Containerfile chmod loop must cover {path}/"
        assert "for d in tests skills checks src docs" in content or (
            "tests" in content and "skills" in content and "chmod g+w" in content
        ), "Containerfile must chmod g+w the L3 source-mode allowlist directories"
        assert "chmod -R g+w" in content, (
            "Containerfile must chmod -R g+w allowlist trees so existing "
            "root-owned files are overwritable, not only new files in g+w dirs"
        )


class TestCheckNoOpenSelfImprovePr:
    def test_passes_when_no_open_prs(self):
        with patch(
            "agentit.portal.github_pr.list_pull_requests", return_value=[],
        ):
            passed, _detail = check_no_open_self_improve_pr()
        assert passed is True

    def test_fails_when_at_or_over_cap(self):
        with patch(
            "agentit.portal.github_pr.list_pull_requests",
            return_value=[{
                "pr_url": "https://github.com/org/agentit/pull/1",
                "title": "x",
                "headRefName": "agentit/self-improve/some-proposal-1784150389",
                "state": "open",
            }],
        ):
            passed, detail = check_no_open_self_improve_pr(max_open_prs=1)
        assert passed is False
        assert "1" in detail

    def test_real_branch_names_have_a_slug_and_timestamp_suffix_and_still_count(self):
        """Prefix filter (not exact head match) must count timestamped
        ``agentit/self-improve/<slug>-<unix-timestamp>`` branches — the
        historical ``gh pr list --head`` exact-match bug silently disabled
        this cap in production."""
        with patch(
            "agentit.portal.github_pr.list_pull_requests",
            return_value=[{
                "pr_url": "https://github.com/alimobrem/AgentIT/pull/15",
                "title": "x",
                "headRefName": "agentit/self-improve/add-failure-alerting-1784150389",
                "state": "open",
            }],
        ) as mock_list:
            passed, detail = check_no_open_self_improve_pr(max_open_prs=1)
        assert mock_list.call_args.kwargs.get("head_prefix") == "agentit/self-improve/"
        assert passed is False
        assert "1" in detail

    def test_prs_with_unrelated_branch_names_are_not_counted(self):
        # list_pull_requests already applies head_prefix; an empty return
        # means no self-improve heads matched.
        with patch("agentit.portal.github_pr.list_pull_requests", return_value=[]):
            passed, _detail = check_no_open_self_improve_pr(max_open_prs=1)
        assert passed is True

    def test_fails_closed_when_github_api_unavailable(self):
        with patch(
            "agentit.portal.github_pr.list_pull_requests",
            side_effect=RuntimeError("GITHUB_TOKEN env var not set — cannot create PR"),
        ):
            passed, detail = check_no_open_self_improve_pr()
        assert passed is False
        assert "github api" in detail.lower() or "token" in detail.lower()

    def test_fails_closed_when_github_api_returns_error(self):
        with patch(
            "agentit.portal.github_pr.list_pull_requests",
            side_effect=RuntimeError("list_pull_requests failed: not authenticated"),
        ):
            passed, detail = check_no_open_self_improve_pr()
        assert passed is False
        assert "not authenticated" in detail


class TestRunSafetyGates:
    def test_all_gates_pass_produces_passed_true(self, tmp_path):
        diff = build_diff(_proposal())
        with patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
             patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")):
            result = run_safety_gates(_proposal(), diff, tmp_path)

        assert result["passed"] is True
        assert len(result["gates"]) == 8
        assert "evidence-usefulness" in [g["name"] for g in result["gates"]]
        assert all(g["passed"] for g in result["gates"])

    def test_one_failing_gate_fails_the_whole_run(self, tmp_path):
        diff = build_diff(_proposal(test_plan=""))
        with patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
             patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")):
            result = run_safety_gates(_proposal(test_plan=""), diff, tmp_path)

        assert result["passed"] is False
        failed = [g["name"] for g in result["gates"] if not g["passed"]]
        assert "test-plan-required" in failed

    def test_out_of_scope_file_is_gate_blocked(self, tmp_path):
        diff = {"chart/templates/foo.yaml": "malicious"}
        with patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
             patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")):
            result = run_safety_gates(_proposal(), diff, tmp_path)

        assert result["passed"] is False
        failed = [g["name"] for g in result["gates"] if not g["passed"]]
        assert "scope-allowlist" in failed

    def test_gate_raising_an_exception_is_caught_and_recorded_as_failed(self, tmp_path):
        diff = build_diff(_proposal())
        with patch("agentit.capability_scout.check_no_secrets", side_effect=RuntimeError("boom")), \
             patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
             patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")):
            result = run_safety_gates(_proposal(), diff, tmp_path)

        assert result["passed"] is False
        no_secrets_gate = next(g for g in result["gates"] if g["name"] == "no-secrets")
        assert no_secrets_gate["passed"] is False
        assert "boom" in no_secrets_gate["detail"]


# ── describe_capability_run ─────────────────────────────────────────────────


class TestDescribeCapabilityRun:
    def test_action_name_is_stable(self):
        assert CAPABILITY_RUN_ACTION == "capability-run"

    def test_error_outcome(self):
        severity, summary, details = describe_capability_run({"doc_gaps": []}, None, None, None, error="no credentials")
        assert severity == "error"
        assert "no credentials" in summary
        assert details["error"] == "no credentials"

    def test_proposed_outcome_with_pr_url(self):
        proposal = _proposal()
        severity, summary, details = describe_capability_run(
            {"doc_gaps": []}, proposal, {"passed": True, "gates": []}, "https://github.com/org/agentit/pull/7",
        )
        assert severity == "info"
        assert "https://github.com/org/agentit/pull/7" in summary
        assert details["pr_url"] == "https://github.com/org/agentit/pull/7"
        assert details["risk"] == "low"

    def test_gate_blocked_outcome_names_failed_gates(self):
        proposal = _proposal()
        gate_result = {"passed": False, "gates": [
            {"name": "test-plan-required", "passed": False, "detail": "no test plan"},
            {"name": "diff-size", "passed": True, "detail": "ok"},
        ]}
        severity, summary, details = describe_capability_run({"doc_gaps": []}, proposal, gate_result, None)
        assert severity == "warning"
        assert "test-plan-required" in summary
        assert details["gate_results"] == gate_result["gates"]

    def test_no_signal_outcome_below_minimum_rows(self):
        severity, summary, _details = describe_capability_run(
            {"doc_gaps": [], "signal_count": MIN_SIGNAL_ROWS - 1}, None, None, None,
        )
        assert severity == "warning"
        assert "insufficient real signal" in summary

    def test_no_proposal_outcome_with_enough_signal(self):
        severity, summary, _details = describe_capability_run(
            {"doc_gaps": [], "signal_count": MIN_SIGNAL_ROWS + 5},
            {"has_proposal": False}, None, None,
        )
        assert severity == "warning"
        assert "no evidence-grounded gap" in summary

    def test_doc_anchor_derived_from_first_doc_gap(self):
        evidence = {"doc_gaps": [{"file": "README.md", "line_no": 42, "anchor": "Known gap", "text": "..."}]}
        _severity, _summary, details = describe_capability_run(evidence, None, None, None)
        assert details["doc_anchor"] == "README.md:42"
