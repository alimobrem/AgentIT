"""Tests for the auto-mode decision engine."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from conftest import make_store, make_report


class TestAutoModeToggle:
    def test_disabled_by_default(self):
        store = make_store()
        auto = AutoMode(store=store)
        assert auto.enabled is False

    def test_enabled_via_env(self):
        store = make_store()
        auto = AutoMode(store=store)
        with patch.dict("os.environ", {"AGENTIT_AUTO_MODE": "true"}):
            assert auto.enabled is True

    def test_enabled_via_store(self):
        store = make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store)
        assert auto.enabled is True

    def test_disabled_via_store(self):
        store = make_store()
        store.set_setting("auto_mode", "false")
        auto = AutoMode(store=store)
        assert auto.enabled is False


class TestShouldAutoApply:
    def test_disabled_returns_false(self):
        store = make_store()
        auto = AutoMode(store=store)
        ok, reason = auto.should_auto_apply(True, ["apiVersion: v1"], "low", "app")
        assert ok is False
        assert "disabled" in reason

    def test_no_auto_approve_returns_false(self):
        store = make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store)
        ok, reason = auto.should_auto_apply(False, ["x"], "high", "app")
        assert ok is False
        assert "human approval" in reason

    def test_no_llm_returns_false(self):
        store = make_store()
        store.set_setting("auto_mode", "true")
        auto = AutoMode(store=store, llm_client=None)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "LLM unavailable" in reason

    def test_llm_says_destructive_returns_false(self):
        store = make_store()
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
        store = make_store()
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
        store = make_store()
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
        store = make_store()
        store.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = None
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "failed" in reason


class TestExecute:
    def test_gates_when_not_auto_approved(self):
        store = make_store()
        store.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = store.save(report)

        auto = AutoMode(store=store, llm_client=None)
        result = auto.execute(aid, [], "default", "high", False, "app")
        assert result["action"] == "gated"
        gates = store.list_gates(status="pending")
        assert len(gates) >= 1

    def test_gates_when_destructive(self):
        store = make_store()
        store.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
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

    def test_decision_event_logged_under_generic_auto_mode_by_default(self):
        """Without an explicit agent_name, the decision event's agent_id stays
        'auto-mode' — the historical/default behavior for callers that don't
        know which agent/skill produced the manifests being classified."""
        store = make_store()
        store.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = store.save(report)

        auto = AutoMode(store=store, llm_client=None)
        auto.execute(aid, [], "default", "high", False, "app")

        events = store.list_events_by_action("decision")
        assert len(events) == 1
        assert events[0]["agent_id"] == "auto-mode"

    def test_decision_event_attributed_to_real_agent_when_supplied(self):
        """When the caller knows the originating agent/skill (e.g. the
        dispatcher's result["agent"]), the decision is logged under that real
        name instead of the generic 'auto-mode' component name."""
        store = make_store()
        store.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = store.save(report)

        auto = AutoMode(store=store, llm_client=None)
        auto.execute(aid, [], "default", "high", False, "app", agent_name="HardeningAgent")

        events = store.list_events_by_action("decision")
        assert len(events) == 1
        assert events[0]["agent_id"] == "HardeningAgent"


class TestSettings:
    def test_get_set_setting(self):
        store = make_store()
        assert store.get_setting("auto_mode") is None
        store.set_setting("auto_mode", "true")
        assert store.get_setting("auto_mode") == "true"
        store.set_setting("auto_mode", "false")
        assert store.get_setting("auto_mode") == "false"

    def test_list_settings(self):
        store = make_store()
        store.set_setting("auto_mode", "true")
        store.set_setting("theme", "dark")
        settings = store.list_settings()
        assert len(settings) == 2
        keys = {s["key"] for s in settings}
        assert "auto_mode" in keys
        assert "theme" in keys
