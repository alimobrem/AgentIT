"""Tests for capability_scout.py's pure research/propose/gate logic --
the counterpart to test_learning_agent.py, but for the loop that proposes
changes to AgentIT's own codebase rather than the skills catalog. See
docs/self-improvement-for-agentit.md and tests/test_capability_scout_watcher.py
for the watcher class itself."""
from __future__ import annotations

import subprocess
from pathlib import Path
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
            _proposal(target_files=["src/agentit/portal/store.py"]), "auto",
        ) == "docs"

    def test_source_mode_falls_back_to_docs_when_targets_ineligible(self):
        from agentit.capability_scout import resolve_build_mode

        assert resolve_build_mode(
            _proposal(target_files=["src/agentit/llm.py", "chart/values.yaml"]), "source",
        ) == "docs"


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

    def test_build_diff_source_returns_docs_fallback_when_llm_returns_nothing(self, tmp_path):
        from agentit.capability_scout import build_diff

        llm = MagicMock()
        llm.generate_capability_files.return_value = None
        proposal = _proposal(title="Fallback", target_files=["skills/security/x.md"])
        diff = build_diff(proposal, mode="source", repo_dir=tmp_path, llm_client=llm)
        assert list(diff) == ["docs/proposals/fallback.md"]


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


class TestCheckNoOpenSelfImprovePr:
    def test_passes_when_no_open_prs(self):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            passed, _detail = check_no_open_self_improve_pr()
        assert passed is True

    def test_fails_when_at_or_over_cap(self):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='[{"url": "https://github.com/org/agentit/pull/1", '
                       '"headRefName": "agentit/self-improve/some-proposal-1784150389"}]',
                stderr="",
            )
            passed, detail = check_no_open_self_improve_pr(max_open_prs=1)
        assert passed is False
        assert "1" in detail

    def test_real_branch_names_have_a_slug_and_timestamp_suffix_and_still_count(self):
        """Regression test for a real bug found live: `gh pr list --head
        agentit/self-improve` does an *exact* branch-name match (confirmed
        against the real repo), but every real branch this loop creates is
        `agentit/self-improve/<slug>-<unix-timestamp>` (see _open_pr) --
        never the literal string `agentit/self-improve`. That made this
        gate's `gh pr list` call always return zero PRs regardless of how
        many were actually open, silently disabling the cap entirely. A
        real open PR with a real timestamped branch name must still be
        counted, and the command itself must not filter by --head at all
        (the filtering happens client-side by prefix instead)."""
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='[{"url": "https://github.com/alimobrem/AgentIT/pull/15", '
                       '"headRefName": "agentit/self-improve/add-failure-alerting-1784150389"}]',
                stderr="",
            )
            passed, detail = check_no_open_self_improve_pr(max_open_prs=1)
        args, _kwargs = mock_run.call_args
        command = args[0]
        assert "--head" not in command
        assert passed is False
        assert "1" in detail

    def test_prs_with_unrelated_branch_names_are_not_counted(self):
        with patch("agentit.capability_scout.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='[{"url": "https://github.com/org/agentit/pull/2", '
                       '"headRefName": "someone-elses-feature-branch"}]',
                stderr="",
            )
            passed, _detail = check_no_open_self_improve_pr(max_open_prs=1)
        assert passed is True

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
