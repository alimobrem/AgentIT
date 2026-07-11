"""Tests for the auto-mode decision engine."""

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


def _make_store() -> AssessmentStore:
    return AssessmentStore(db_path=":memory:")


def _make_report() -> AssessmentReport:
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


class TestAutoModeToggle:
    def test_disabled_by_default(self):
        store = _make_store()
        auto = AutoMode(store=store)
        assert auto.enabled is False

    def test_enabled_via_env(self):
        store = _make_store()
        auto = AutoMode(store=store)
        with patch.dict("os.environ", {"AGENTIT_AUTO_MODE": "true"}):
            assert auto.enabled is True

    def test_enabled_via_store(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store)
        assert auto.enabled is True

    def test_disabled_via_store(self):
        store = _make_store()
        store.set_setting("auto_mode", "false")
        auto = AutoMode(store=store)
        assert auto.enabled is False


class TestShouldAutoApply:
    def test_disabled_returns_false(self):
        store = _make_store()
        auto = AutoMode(store=store)
        ok, reason = auto.should_auto_apply(True, ["apiVersion: v1"], "low", "app")
        assert ok is False
        assert "disabled" in reason

    def test_no_auto_approve_returns_false(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store)
        ok, reason = auto.should_auto_apply(False, ["x"], "high", "app")
        assert ok is False
        assert "human approval" in reason

    def test_no_llm_returns_false(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store, llm_client=None)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "LLM unavailable" in reason

    def test_llm_says_destructive_returns_false(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": True,
            "confidence": 0.95,
            "reason": "Removes NetworkPolicy",
        }
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "destructive" in reason

    def test_llm_low_confidence_returns_false(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.5,
            "reason": "Unclear",
        }
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "confidence" in reason

    def test_llm_says_safe_returns_true(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.95,
            "reason": "Adds new ConfigMap",
        }
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is True
        assert "safe" in reason

    def test_llm_returns_none_fails_closed(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = None
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "failed" in reason


class TestExecute:
    def test_gates_when_not_auto_approved(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        report = _make_report()
        aid = store.save(report)

        auto = AutoMode(store=store, llm_client=None)
        result = auto.execute(aid, [], "default", "high", False, "app")
        assert result["action"] == "gated"
        gates = store.list_gates(status="pending")
        assert len(gates) >= 1

    def test_gates_when_destructive(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        report = _make_report()
        aid = store.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": True,
            "confidence": 0.9,
            "reason": "Deletes resources",
        }
        auto = AutoMode(store=store, llm_client=llm)
        result = auto.execute(
            aid, [{"path": "x.yaml", "content": "kind: Pod"}],
            "default", "low", True, "app",
        )
        assert result["action"] == "gated"


class TestSettings:
    def test_get_set_setting(self):
        store = _make_store()
        assert store.get_setting("auto_mode") is None
        store.set_setting("auto_mode", "true")
        assert store.get_setting("auto_mode") == "true"
        store.set_setting("auto_mode", "false")
        assert store.get_setting("auto_mode") == "false"

    def test_list_settings(self):
        store = _make_store()
        store.set_setting("auto_mode", "true")
        store.set_setting("theme", "dark")
        settings = store.list_settings()
        assert len(settings) == 2
        keys = {s["key"] for s in settings}
        assert "auto_mode" in keys
        assert "theme" in keys
