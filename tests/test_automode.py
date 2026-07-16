"""Tests for the auto-mode decision engine."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from conftest import make_async_store, make_report


class TestAutoModeToggle:
    async def test_disabled_by_default(self):
        store, _raw = await make_async_store()
        auto = AutoMode(store=store)
        assert await auto.is_enabled() is False

    async def test_enabled_via_env(self):
        store, _raw = await make_async_store()
        auto = AutoMode(store=store)
        with patch.dict("os.environ", {"AGENTIT_AUTO_MODE": "true"}):
            assert await auto.is_enabled() is True

    async def test_enabled_via_store(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        auto = AutoMode(store=store)
        assert await auto.is_enabled() is True

    async def test_disabled_via_store(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "false")
        auto = AutoMode(store=store)
        assert await auto.is_enabled() is False


class TestShouldAutoApply:
    async def test_disabled_returns_false(self):
        store, _raw = await make_async_store()
        auto = AutoMode(store=store)
        ok, reason = await auto.should_auto_apply(True, ["apiVersion: v1"], "low", "app")
        assert ok is False
        assert "disabled" in reason

    async def test_no_auto_approve_returns_false(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        auto = AutoMode(store=store)
        ok, reason = await auto.should_auto_apply(False, ["x"], "high", "app")
        assert ok is False
        assert "human approval" in reason

    async def test_no_llm_returns_false(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        auto = AutoMode(store=store, llm_client=None)
        ok, reason = await auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "LLM unavailable" in reason

    async def test_llm_says_destructive_returns_false(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": True,
            "confidence": 0.95,
            "reason": "Removes NetworkPolicy",
        }
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = await auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "destructive" in reason

    async def test_llm_low_confidence_returns_false(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.5,
            "reason": "Unclear",
        }
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = await auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "confidence" in reason

    async def test_llm_says_safe_returns_true(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.95,
            "reason": "Adds new ConfigMap",
        }
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = await auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is True
        assert "safe" in reason

    async def test_llm_returns_none_fails_closed(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        llm = MagicMock()
        llm.classify_action.return_value = None
        auto = AutoMode(store=store, llm_client=llm)
        ok, reason = await auto.should_auto_apply(True, ["x"], "low", "app")
        assert ok is False
        assert "failed" in reason


class TestExecute:
    async def test_gates_when_not_auto_approved(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        auto = AutoMode(store=store, llm_client=None)
        result = await auto.execute(aid, [], "default", "high", False, "app")
        assert result["action"] == "gated"
        gates = await raw.list_gates(status="pending")
        assert len(gates) >= 1

    async def test_gates_when_destructive(self):
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": True,
            "confidence": 0.9,
            "reason": "Deletes resources",
        }
        auto = AutoMode(store=store, llm_client=llm)
        result = await auto.execute(
            aid, [{"path": "x.yaml", "content": "kind: Pod"}],
            "default", "low", True, "app",
        )
        assert result["action"] == "gated"

    async def test_decision_event_logged_under_generic_auto_mode_by_default(self):
        """Without an explicit agent_name, the decision event's agent_id stays
        'auto-mode' — the historical/default behavior for callers that don't
        know which agent/skill produced the manifests being classified."""
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        auto = AutoMode(store=store, llm_client=None)
        await auto.execute(aid, [], "default", "high", False, "app")

        events = await raw.list_events_by_action("decision")
        assert len(events) == 1
        assert events[0]["agent_id"] == "auto-mode"

    async def test_decision_event_attributed_to_real_agent_when_supplied(self):
        """When the caller knows the originating agent/skill (e.g. the
        dispatcher's result["agent"]), the decision is logged under that real
        name instead of the generic 'auto-mode' component name."""
        store, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        auto = AutoMode(store=store, llm_client=None)
        await auto.execute(aid, [], "default", "high", False, "app", agent_name="HardeningAgent")

        events = await raw.list_events_by_action("decision")
        assert len(events) == 1
        assert events[0]["agent_id"] == "HardeningAgent"


class TestSettings:
    async def test_get_set_setting(self):
        """Direct sync store calls — unaffected by AutoMode's async conversion."""
        from conftest import make_store
        store = await make_store()
        assert await store.get_setting("auto_mode") is None
        await store.set_setting("auto_mode", "true")
        assert await store.get_setting("auto_mode") == "true"
        await store.set_setting("auto_mode", "false")
        assert await store.get_setting("auto_mode") == "false"

    async def test_list_settings(self):
        from conftest import make_store
        store = await make_store()
        await store.set_setting("auto_mode", "true")
        await store.set_setting("theme", "dark")
        settings = await store.list_settings()
        assert len(settings) == 2
        keys = {s["key"] for s in settings}
        assert "auto_mode" in keys
        assert "theme" in keys
