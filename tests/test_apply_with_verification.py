"""Tests for the shared apply-with-verification orchestration
(``cluster_apply.apply_with_verification``) -- the sequence factored out of
both the manual "Apply to Cluster" route and ``AutoMode.execute()``.

Covers the one real behavioral difference between the two callers
(``force_dry_run_first``), the consolidated side effects (``record_skill_
outcomes()``, ``audit_log()``), and the audit-log gap closed for AutoMode.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from agentit.portal.cluster_apply import apply_with_verification
from conftest import make_async_store, make_report


def _skill_file(path: str = "app-network-policy.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


class TestManualModeNoForcedDryRun:
    """``force_dry_run_first=False`` -- exactly one call to
    ``apply_manifests_to_cluster``, matching the manual route's pre-refactor
    "just do what the human asked" behavior."""

    async def test_dry_run_true_makes_single_dry_run_call(self):
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            result = await apply_with_verification(
                [_skill_file()], "ns", True,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user@example.com", action="apply-to-cluster", resource="assessment:1",
            )

        mock_apply.assert_called_once_with([_skill_file()], "ns", True)
        assert result["is_dry_run"] is True
        assert result["dry_run_failed"] is False

    async def test_dry_run_false_applies_directly_no_dry_run_first(self):
        """No automatic dry-run-first sequencing: dry_run=False makes exactly
        one real-apply call, never a preceding dry-run call."""
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user@example.com", action="apply-to-cluster", resource="assessment:1",
            )

        assert mock_apply.call_count == 1
        mock_apply.assert_called_once_with([_skill_file()], "ns", False)
        assert result["is_dry_run"] is False
        assert result["dry_run_failed"] is False

    async def test_dry_run_true_never_records_skill_outcomes(self):
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply, \
             patch("agentit.portal.cluster_apply.record_skill_outcomes") as mock_record:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            await apply_with_verification(
                [_skill_file()], "ns", True,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user", action="apply-to-cluster", resource="assessment:1",
            )
        mock_record.assert_not_called()

    async def test_real_apply_records_skill_outcomes_even_with_partial_errors(self):
        """Manual route behavior: any real apply (dry_run=False) records
        outcomes for whatever *did* succeed, even if other files in the same
        batch errored -- ``record_outcomes_on_partial_failure`` defaults to
        True to preserve this."""
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply, \
             patch("agentit.portal.cluster_apply.record_skill_outcomes") as mock_record:
            mock_apply.return_value = {
                "applied": ["app-network-policy.yaml"], "skipped": [], "errors": ["other.yaml: boom"],
            }
            await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="applied to cluster",
                actor="user", action="apply-to-cluster", resource="assessment:1",
            )
        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[0] is store
        assert args[1] == "app"
        assert args[3] == {"app-network-policy.yaml"}
        assert args[4] == "approved"

    async def test_audit_log_called_for_every_call_dry_or_real(self, caplog):
        store, _raw = make_async_store()
        for dry_run in (True, False):
            with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
                mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
                with caplog.at_level(logging.INFO, logger="agentit.audit"):
                    await apply_with_verification(
                        [_skill_file()], "ns", dry_run,
                        force_dry_run_first=False,
                        store=store, app_name="app",
                        skill_outcome_reason="r",
                        actor="user@example.com", action="apply-to-cluster", resource="assessment:1",
                    )
            audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
            assert len(audit_records) == 1
            assert audit_records[0].actor == "user@example.com"
            assert audit_records[0].outcome == "success"
            caplog.clear()

    async def test_audit_log_outcome_partial_on_errors(self, caplog):
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["boom"]}
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                await apply_with_verification(
                    [_skill_file()], "ns", False,
                    force_dry_run_first=False,
                    store=store, app_name="app",
                    skill_outcome_reason="r",
                    actor="user", action="apply-to-cluster", resource="assessment:1",
                )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "partial"

    async def test_exception_from_apply_is_audited_then_reraised(self, caplog):
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.side_effect = RuntimeError("cluster unreachable")
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                with pytest.raises(RuntimeError, match="cluster unreachable"):
                    await apply_with_verification(
                        [_skill_file()], "ns", False,
                        force_dry_run_first=False,
                        store=store, app_name="app",
                        skill_outcome_reason="r",
                        actor="user", action="apply-to-cluster", resource="assessment:1",
                    )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "error"


class TestAutoModeForcedDryRunFirst:
    """``force_dry_run_first=True`` -- always dry-run first regardless of the
    ``dry_run`` argument, gate on dry-run errors, only then apply for real.
    This is AutoMode's own always-on safety behavior."""

    async def test_dry_run_called_first_then_real_apply(self):
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=True,
                store=store, app_name="app",
                skill_outcome_reason="r",
                record_outcomes_on_partial_failure=False,
                actor="auto-mode", action="auto-apply", resource="assessment:1",
            )

        assert mock_apply.call_count == 2
        first_call, second_call = mock_apply.call_args_list
        assert first_call.args[2] is True  # dry-run first
        assert second_call.args[2] is False  # then a real apply
        assert result["is_dry_run"] is False
        assert result["dry_run_failed"] is False

    async def test_dry_run_failure_gates_before_real_apply(self):
        """If the forced first dry-run reports errors, the real apply is
        never attempted at all."""
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["forbidden"]}
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=True,
                store=store, app_name="app",
                skill_outcome_reason="r",
                record_outcomes_on_partial_failure=False,
                actor="auto-mode", action="auto-apply", resource="assessment:1",
            )

        mock_apply.assert_called_once_with([_skill_file()], "ns", True)
        assert result["dry_run_failed"] is True
        assert result["is_dry_run"] is True
        assert result["errors"] == ["forbidden"]

    async def test_record_outcomes_on_partial_failure_false_skips_recording(self):
        """AutoMode's pre-refactor behavior: if the real apply (after passing
        the dry-run gate) has any errors, no skill outcome is recorded at
        all -- even for files that did succeed."""
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        call_results = [
            {"applied": [], "skipped": [], "errors": []},  # dry-run: clean
            {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": ["other: boom"]},  # real apply: partial
        ]
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster", side_effect=call_results) as mock_apply, \
             patch("agentit.portal.cluster_apply.record_skill_outcomes") as mock_record:
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=True,
                store=store, app_name="app",
                skill_outcome_reason="r",
                record_outcomes_on_partial_failure=False,
                actor="auto-mode", action="auto-apply", resource="assessment:1",
            )

        assert mock_apply.call_count == 2
        mock_record.assert_not_called()
        assert result["errors"] == ["other: boom"]

    async def test_clean_real_apply_records_skill_outcomes(self):
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        call_results = [
            {"applied": [], "skipped": [], "errors": []},
            {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []},
        ]
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster", side_effect=call_results) as mock_apply, \
             patch("agentit.portal.cluster_apply.record_skill_outcomes") as mock_record:
            await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=True,
                store=store, app_name="app",
                skill_outcome_reason="LLM classified as safe",
                record_outcomes_on_partial_failure=False,
                actor="auto-mode", action="auto-apply", resource="assessment:1",
            )

        assert mock_apply.call_count == 2
        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[3] == {"app-network-policy.yaml"}
        assert args[4] == "approved"
        assert args[5] == "LLM classified as safe"

    async def test_audit_log_gap_closed_dry_run_failure(self, caplog):
        """Real gap fix: AutoMode previously never called audit_log() at
        all. Now the dry-run-gate branch is audited too."""
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["forbidden"]}
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                await apply_with_verification(
                    [_skill_file()], "ns", False,
                    force_dry_run_first=True,
                    store=store, app_name="app",
                    skill_outcome_reason="r",
                    record_outcomes_on_partial_failure=False,
                    actor="auto-mode", action="auto-apply", resource="assessment:1",
                )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].actor == "auto-mode"
        assert audit_records[0].action == "auto-apply"
        assert audit_records[0].outcome == "dry-run-failed"

    async def test_audit_log_gap_closed_successful_auto_apply(self, caplog):
        """Real gap fix: a clean auto-applied real apply now leaves an audit
        trail too (previously none at all for AutoMode)."""
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        call_results = [
            {"applied": [], "skipped": [], "errors": []},
            {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []},
        ]
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster", side_effect=call_results):
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                await apply_with_verification(
                    [_skill_file()], "ns", False,
                    force_dry_run_first=True,
                    store=store, app_name="app",
                    skill_outcome_reason="r",
                    record_outcomes_on_partial_failure=False,
                    actor="auto-mode", action="auto-apply", resource="assessment:1",
                )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].actor == "auto-mode"
        assert audit_records[0].action == "auto-apply"
        assert audit_records[0].outcome == "success"


class TestConflictVsOtherFailureDistinction:
    """`apply_manifests_to_cluster()`'s `conflicts` list (kube.apply_yaml()'s
    structured 409 result) must be tracked distinctly from `errors`
    throughout `apply_with_verification()` -- never silently lumped into a
    generic failure, and never silently ignored."""

    def _conflict_result(self, applied=None, conflicts=None):
        return {
            "applied": applied or [], "skipped": [], "errors": [],
            "conflicts": conflicts or [{"path": "x.yaml", "error": "conflict", "details": []}],
        }

    async def test_dry_run_conflict_gates_before_real_apply(self):
        """A conflict surfaced during AutoMode's forced dry-run must gate
        exactly like a dry-run error -- the real apply is never attempted."""
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result()
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=True,
                store=store, app_name="app",
                skill_outcome_reason="r",
                record_outcomes_on_partial_failure=False,
                actor="auto-mode", action="auto-apply", resource="assessment:1",
            )

        mock_apply.assert_called_once_with([_skill_file()], "ns", True)
        assert result["dry_run_failed"] is True
        assert result["conflicts"]

    async def test_real_apply_conflict_does_not_count_as_error(self):
        store, raw = make_async_store()
        report = make_report()
        raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result(applied=["ok.yaml"])
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user", action="apply-to-cluster", resource="assessment:1",
            )

        assert result["errors"] == []
        assert len(result["conflicts"]) == 1

    async def test_audit_log_outcome_is_conflict_not_partial_or_success(self, caplog):
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result()
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                await apply_with_verification(
                    [_skill_file()], "ns", False,
                    force_dry_run_first=False,
                    store=store, app_name="app",
                    skill_outcome_reason="r",
                    actor="user", action="apply-to-cluster", resource="assessment:1",
                )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "conflict"

    async def test_force_default_false_never_passed_to_apply_manifests(self):
        """`force` defaults to False and, left at that default, is never
        passed through at all -- matching `allow_operator_namespaces`'s
        existing "byte-for-byte identical mocked calls" convention."""
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user", action="apply-to-cluster", resource="assessment:1",
            )
        mock_apply.assert_called_once_with([_skill_file()], "ns", False)

    async def test_force_true_is_threaded_through(self):
        store, _raw = make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            await apply_with_verification(
                [_skill_file()], "ns", False,
                force_dry_run_first=False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user", action="apply-to-cluster", resource="assessment:1",
                force=True,
            )
        assert mock_apply.call_args.kwargs == {"force": True}
