"""Extended auto-mode tests — execute pipeline, LLM integration, edge cases."""

from __future__ import annotations

import logging

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from agentit.llm import LLMClient
from conftest import make_async_store, make_report


class TestExecuteAutoApply:
    async def test_auto_apply_with_safe_llm(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.95,
            "reason": "Adds ConfigMap",
        }

        files = [
            {"category": "cost", "path": "labels.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
             "description": "labels"},
        ]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["labels.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        assert "safe" in result["reason"]

    async def test_dry_run_failure_gates(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.95,
            "reason": "Safe",
        }

        files = [{"category": "sec", "path": "np.yaml",
                  "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
                  "description": "np"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["forbidden"]}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "dry-run" in result["reason"]

    async def test_marks_remediations_complete_on_apply(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)
        raw.save_remediation(aid, "security", "Add NetworkPolicy")

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            await engine.execute(aid, [{"path": "np.yaml", "content": "x", "category": "sec", "description": "np"}],
                                  "default", "low", True, "test-app")

        rems = raw.list_remediations(aid)
        assert rems[0]["status"] == "completed"


class TestExecuteDryRunFirstAlwaysEnforced:
    """AutoMode's real, deliberately-preserved distinction from the manual
    "Apply to Cluster" route: it always dry-runs first, regardless of what
    the eventual real-apply outcome would be, and never skips straight to a
    real apply."""

    async def test_dry_run_called_before_real_apply(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        assert mock_apply.call_count == 2
        first_call, second_call = mock_apply.call_args_list
        assert first_call.args[2] is True, "first call must be a dry-run"
        assert second_call.args[2] is False, "second call must be the real apply"


class TestExecuteAuditLogGapClosed:
    """Real gap fix: before this refactor, AutoMode.execute() never called
    audit_log() at all (only the manual route did). These confirm the
    shared apply_with_verification() closes that for every real exit path."""

    async def test_audit_log_fires_on_successful_auto_apply(self, caplog):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        files = [{"category": "cost", "path": "labels.yaml", "content": "x", "description": "labels"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["labels.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].actor == "auto-mode"
        assert audit_records[0].action == "auto-apply"
        assert audit_records[0].resource == f"assessment:{aid}"
        assert audit_records[0].outcome == "success"

    async def test_audit_log_fires_on_dry_run_failure_gate(self, caplog):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["forbidden"]}
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "dry-run-failed"

    async def test_no_audit_log_when_gated_before_apply_attempted(self, caplog):
        """auto_approve=False gates before apply_with_verification is ever
        called -- no cluster-apply audit entry should appear (there was
        nothing to audit yet)."""
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        engine = AutoMode(store=s, llm_client=None)
        with caplog.at_level(logging.INFO, logger="agentit.audit"):
            result = await engine.execute(aid, [], "default", "high", False, "app")

        assert result["action"] == "gated"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 0


class TestExecuteWithPublisher:
    async def test_publishes_events(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        publisher = MagicMock()
        engine = AutoMode(store=s, publisher=publisher, llm_client=None)
        await engine.execute(aid, [], "default", "high", False, "test-app")

        assert publisher.publish.called
        call_args = publisher.publish.call_args
        assert call_args[1]["agent_id"] == "auto-mode"


class TestLLMClassifyAction:
    def _make_client(self):
        with patch("agentit.llm._create_client"):
            return LLMClient(model="test")

    def test_classify_action_returns_dict(self):
        client = self._make_client()
        with patch.object(client, "_chat", return_value='{"is_destructive": false, "confidence": 0.9, "reason": "Adds ConfigMap"}'):
            result = client.classify_action("apply", ["kind: ConfigMap"], "test context")
        assert result is not None
        assert result["is_destructive"] is False
        assert result["confidence"] == 0.9

    def test_classify_action_returns_none_on_bad_json(self):
        client = self._make_client()
        with patch.object(client, "_chat", return_value="not json"):
            result = client.classify_action("apply", ["kind: Pod"], "ctx")
        assert result is None

    def test_classify_action_returns_none_on_llm_failure(self):
        client = self._make_client()
        with patch.object(client, "_chat", return_value=None):
            result = client.classify_action("apply", ["kind: Pod"], "ctx")
        assert result is None
