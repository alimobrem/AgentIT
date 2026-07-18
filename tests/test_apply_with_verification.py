"""Tests for the shared apply-with-verification orchestration
(``cluster_apply.apply_with_verification``).

Direct Apply has been removed as a concept entirely, and (2026-07-18) so has
this function's own last caller, the ``cluster-admin-review`` gate's
approval path (``routes/gates.py``, for CI/CD manifests destined for a
shared operator namespace -- that category now delivers via a GitOps PR
instead, same as every other category). No live code path calls this
function anymore; it's kept, unreachable, as a real, well-tested, general
"apply YAML manifests to a cluster safely" primitive rather than deleted --
see the function's own docstring. ``force_dry_run_first``/``record_outcomes_
on_partial_failure``/``force`` (all specific to the earlier-removed direct-
apply/``AutoMode``/``cluster-conflict-review`` call shapes) were removed
from this function's signature along with them -- this covers a single
``apply_manifests_to_cluster()`` call, the consolidated side effects
(``record_skill_outcomes()``, ``audit_log()``), and conflict-vs-error
tracking.
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


class TestApplyWithVerification:
    """Exactly one call to ``apply_manifests_to_cluster`` per invocation --
    the ``force_dry_run_first``-driven "dry-run first, then real apply"
    sequencing this used to also support was `AutoMode`'s own, removed
    along with `AutoMode`'s direct-apply branch as a concept entirely (see
    `automode.py`)."""

    async def test_dry_run_true_makes_single_dry_run_call(self):
        store, _raw = await make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            result = await apply_with_verification(
                [_skill_file()], "ns", True,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user@example.com", action="apply-to-cluster", resource="assessment:1",
            )

        mock_apply.assert_called_once_with([_skill_file()], "ns", True)
        assert result["is_dry_run"] is True

    async def test_dry_run_false_applies_directly(self):
        store, _raw = await make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user@example.com", action="apply-to-cluster", resource="assessment:1",
            )

        assert mock_apply.call_count == 1
        mock_apply.assert_called_once_with([_skill_file()], "ns", False)
        assert result["is_dry_run"] is False

    async def test_dry_run_true_never_records_skill_outcomes(self):
        store, raw = await make_async_store()
        report = make_report()
        await raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply, \
             patch("agentit.portal.cluster_apply.record_skill_outcomes") as mock_record:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            await apply_with_verification(
                [_skill_file()], "ns", True,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user", action="apply-to-cluster", resource="assessment:1",
            )
        mock_record.assert_not_called()

    async def test_real_apply_records_skill_outcomes_even_with_partial_errors(self):
        """Any real apply (dry_run=False) records outcomes for whatever
        *did* succeed, even if other files in the same batch errored."""
        store, raw = await make_async_store()
        report = make_report()
        await raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply, \
             patch("agentit.portal.cluster_apply.record_skill_outcomes") as mock_record:
            mock_apply.return_value = {
                "applied": ["app-network-policy.yaml"], "skipped": [], "errors": ["other.yaml: boom"],
            }
            await apply_with_verification(
                [_skill_file()], "ns", False,
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
        store, _raw = await make_async_store()
        for dry_run in (True, False):
            with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
                mock_apply.return_value = {"applied": ["x.yaml"], "skipped": [], "errors": []}
                with caplog.at_level(logging.INFO, logger="agentit.audit"):
                    await apply_with_verification(
                        [_skill_file()], "ns", dry_run,
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
        store, _raw = await make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["boom"]}
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                await apply_with_verification(
                    [_skill_file()], "ns", False,
                    store=store, app_name="app",
                    skill_outcome_reason="r",
                    actor="user", action="apply-to-cluster", resource="assessment:1",
                )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "partial"

    async def test_exception_from_apply_is_audited_then_reraised(self, caplog):
        store, _raw = await make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.side_effect = RuntimeError("cluster unreachable")
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                with pytest.raises(RuntimeError, match="cluster unreachable"):
                    await apply_with_verification(
                        [_skill_file()], "ns", False,
                        store=store, app_name="app",
                        skill_outcome_reason="r",
                        actor="user", action="apply-to-cluster", resource="assessment:1",
                    )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "error"


class TestConflictVsOtherFailureDistinction:
    """`apply_manifests_to_cluster()`'s `conflicts` list (kube.apply_yaml()'s
    structured 409 result) must be tracked distinctly from `errors`
    throughout `apply_with_verification()` -- never silently lumped into a
    generic failure, and never silently ignored. There is no longer a
    `force` parameter to seize ownership on a conflict -- that existed only
    for the removed `cluster-conflict-review` gate type's force-reapply
    (see `automode.py`/`routes/gates.py`); a conflict here now just
    surfaces in `conflicts`, with no force-through path at all."""

    def _conflict_result(self, applied=None, conflicts=None):
        return {
            "applied": applied or [], "skipped": [], "errors": [],
            "conflicts": conflicts or [{"path": "x.yaml", "error": "conflict", "details": []}],
        }

    async def test_real_apply_conflict_does_not_count_as_error(self):
        store, raw = await make_async_store()
        report = make_report()
        await raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result(applied=["ok.yaml"])
            result = await apply_with_verification(
                [_skill_file()], "ns", False,
                store=store, app_name="app",
                skill_outcome_reason="r",
                actor="user", action="apply-to-cluster", resource="assessment:1",
            )

        assert result["errors"] == []
        assert len(result["conflicts"]) == 1

    async def test_audit_log_outcome_is_conflict_not_partial_or_success(self, caplog):
        store, _raw = await make_async_store()
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result()
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                await apply_with_verification(
                    [_skill_file()], "ns", False,
                    store=store, app_name="app",
                    skill_outcome_reason="r",
                    actor="user", action="apply-to-cluster", resource="assessment:1",
                )
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "conflict"
