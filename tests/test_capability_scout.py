"""Tests for capability_scout.py's pure research/propose/gate logic --
the counterpart to test_learning_agent.py, but for the loop that proposes
changes to AgentIT's own codebase rather than the skills catalog. See
docs/self-improvement-for-agentit.md and tests/test_capability_scout_watcher.py
for the watcher class itself."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from agentit.capability_scout import (
    CAPABILITY_RUN_ACTION,
    MAX_DIFF_FILES,
    MAX_DIFF_LINES,
    MIN_SIGNAL_ROWS,
    build_diff,
    check_diff_size,
    check_has_test_plan,
    check_no_open_self_improve_pr,
    check_no_secrets,
    check_scope_allowlist,
    check_syntax,
    describe_capability_run,
    gather_evidence,
    render_proposal_doc,
    run_safety_gates,
    run_test_suite,
    scan_doc_gaps,
    slugify,
)
from conftest import make_async_store


def _proposal(**overrides) -> dict:
    base = {
        "has_proposal": True,
        "title": "Track stack signatures",
        "gap_description": "README documents an idea that was never built",
        "evidence": "README.md:42 — Documented future idea (not built)",
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


# ── gather_evidence ─────────────────────────────────────────────────────────


class TestGatherEvidence:
    async def test_aggregates_real_store_signal(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        async_store, raw_store = make_async_store()
        raw_store.record_feedback("app-a", "hardening", "network-policy", "rejected")
        raw_store.record_feedback("app-b", "hardening", "network-policy", "rejected")

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


class TestRunTestSuite:
    def test_returns_true_when_pytest_exits_zero(self, tmp_path):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            passed, detail = run_test_suite(tmp_path)
        assert passed is True
        assert "passed" in detail

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
        assert "--ignore=tests/test_live_cluster_e2e.py" in command
        assert kwargs["env"]["KUBECONFIG"] == "/tmp/nonexistent-path"


class TestCheckNoOpenSelfImprovePr:
    def test_passes_when_no_open_prs(self):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            passed, _detail = check_no_open_self_improve_pr()
        assert passed is True

    def test_fails_when_at_or_over_cap(self):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='[{"url": "https://github.com/org/agentit/pull/1"}]', stderr="",
            )
            passed, detail = check_no_open_self_improve_pr(max_open_prs=1)
        assert passed is False
        assert "1" in detail

    def test_fails_closed_when_gh_unavailable(self):
        with patch("agentit.capability_scout.subprocess.run", side_effect=OSError("gh not found")):
            passed, detail = check_no_open_self_improve_pr()
        assert passed is False
        assert "gh" in detail.lower()

    def test_fails_closed_when_gh_returns_nonzero(self):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not authenticated")
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
        assert len(result["gates"]) == 7
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
