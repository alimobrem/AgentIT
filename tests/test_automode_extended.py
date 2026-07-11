"""Extended auto-mode tests — execute pipeline, LLM integration, edge cases."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)
from agentit.portal.store import AssessmentStore


def _store() -> AssessmentStore:
    return AssessmentStore(db_path=":memory:")


def _report() -> AssessmentReport:
    return AssessmentReport(
        repo_url="https://github.com/org/test-app",
        repo_name="test-app",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[],
        ),
        scores=[DimensionScore(
            dimension="security", score=80, max_score=100,
            findings=[Finding(category="test", severity=Severity.low,
                              description="minor", recommendation="fix")],
        )],
        criticality="low",
        summary="test",
        remediation_plan=[],
    )


class TestExecuteAutoApply:
    def test_auto_apply_with_safe_llm(self):
        s = _store()
        s.set_setting("auto_mode", "true")
        report = _report()
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
        s = _store()
        s.set_setting("auto_mode", "true")
        report = _report()
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
        s = _store()
        s.set_setting("auto_mode", "true")
        report = _report()
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
        s = _store()
        s.set_setting("auto_mode", "true")
        report = _report()
        aid = s.save(report)

        publisher = MagicMock()
        engine = AutoMode(store=s, publisher=publisher, llm_client=None)
        engine.execute(aid, [], "default", "high", False, "test-app")

        assert publisher.publish.called
        call_args = publisher.publish.call_args
        assert call_args[1]["agent_id"] == "auto-mode"


class TestLLMClassifyAction:
    def test_classify_action_returns_dict(self):
        from agentit.llm import LLMClient
        with patch.object(LLMClient, "_chat", return_value='{"is_destructive": false, "confidence": 0.9, "reason": "Adds ConfigMap"}'):
            client = LLMClient.__new__(LLMClient)
            client.model = "test"
            result = client.classify_action("apply", ["kind: ConfigMap"], "test context")
        assert result is not None
        assert result["is_destructive"] is False
        assert result["confidence"] == 0.9

    def test_classify_action_returns_none_on_bad_json(self):
        from agentit.llm import LLMClient
        with patch.object(LLMClient, "_chat", return_value="not json"):
            client = LLMClient.__new__(LLMClient)
            client.model = "test"
            result = client.classify_action("apply", ["kind: Pod"], "ctx")
        assert result is None

    def test_classify_action_returns_none_on_llm_failure(self):
        from agentit.llm import LLMClient
        with patch.object(LLMClient, "_chat", return_value=None):
            client = LLMClient.__new__(LLMClient)
            client.model = "test"
            result = client.classify_action("apply", ["kind: Pod"], "ctx")
        assert result is None
