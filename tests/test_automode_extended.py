"""Extended auto-mode tests — execute pipeline, LLM integration, edge cases."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from agentit.llm import LLMClient
from conftest import make_store, make_report


class TestExecuteAutoApply:
    def test_auto_apply_with_safe_llm(self):
        s = make_store()
        s.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = s.save(report)

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
            result = engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        assert "safe" in result["reason"]

    def test_dry_run_failure_gates(self):
        s = make_store()
        s.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = s.save(report)

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
            result = engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "dry-run" in result["reason"]

    def test_marks_remediations_complete_on_apply(self):
        s = make_store()
        s.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = s.save(report)
        rid = s.save_remediation(aid, "security", "Add NetworkPolicy")

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            engine.execute(aid, [{"path": "np.yaml", "content": "x", "category": "sec", "description": "np"}],
                           "default", "low", True, "test-app")

        rems = s.list_remediations(aid)
        assert rems[0]["status"] == "completed"


class TestExecuteWithPublisher:
    def test_publishes_events(self):
        s = make_store()
        s.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = s.save(report)

        publisher = MagicMock()
        engine = AutoMode(store=s, publisher=publisher, llm_client=None)
        engine.execute(aid, [], "default", "high", False, "test-app")

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
