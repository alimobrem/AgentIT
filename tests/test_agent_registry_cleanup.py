"""Tests for agent_registry_cleanup.py: pruning stale agent_registry rows.

Mirrors the structure of test_skill_inventory.py, which covers the
analogous skills/checks catalog snapshot-diff-and-log helper this module is
a sibling of.
"""

from __future__ import annotations

from agentit.agent_registry_cleanup import (
    get_known_agent_names,
    prune_stale_agents_and_log,
)
from agentit.agents.capabilities import AGENT_CLASSES, WATCHER_AGENTS
from conftest import make_store


class TestGetKnownAgentNames:
    def test_includes_no_python_onboarding_agents(self) -> None:
        """Every Python onboarding agent, including the last one
        (`codechange`, removed 2026-08-07), is gone -- skills own every
        domain now (see docs/agent-removal-readiness.md). AGENT_CLASSES is
        always empty, so the known-names set is exactly the watcher set."""
        known = get_known_agent_names()
        assert known == frozenset(w["name"] for w in WATCHER_AGENTS)

    def test_includes_long_lived_watchers(self) -> None:
        known = get_known_agent_names()
        assert "vuln-watcher" in known
        assert "slo-tracker" in known
        assert "drift-detector" in known
        assert "skill-learner" in known

    def test_excludes_removed_python_agents(self) -> None:
        """Every Python agent removed in favor of skills-only generation
        must never appear in the known-names set, or their stale
        agent_registry rows would never get pruned."""
        known = get_known_agent_names()
        removed = {
            "chaos", "cicd", "compliance", "hardening", "incident",
            "infrastructure", "observability", "release", "retirement",
            "cost", "dependency", "codechange",
        }
        assert known.isdisjoint(removed)

    def test_matches_capabilities_registries_exactly(self) -> None:
        """No hand-maintained duplicate list -- this must always reflect
        whatever agents/capabilities.py currently declares."""
        expected = frozenset(AGENT_CLASSES) | frozenset(w["name"] for w in WATCHER_AGENTS)
        assert get_known_agent_names() == expected


class TestPruneStaleAgentsAndLog:
    async def test_prunes_stale_rows_and_logs_event(self) -> None:
        store = await make_store()
        for name in ("chaos", "cicd", "security"):
            await store.register_agent(name, name)
        await store.agent_heartbeat("vuln-watcher")

        pruned = await prune_stale_agents_and_log(store)

        assert sorted(pruned) == ["chaos", "cicd", "security"]
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"vuln-watcher"}

        events = await store.list_events_by_agent("agent-registry")
        assert len(events) == 1
        assert events[0]["action"] == "agent-registry-pruned"
        assert events[0]["severity"] == "warning"
        assert "chaos" in events[0]["summary"]

    async def test_preserves_legitimate_watchers(self) -> None:
        """Long-lived watchers must never be pruned."""
        store = await make_store()
        await store.agent_heartbeat("vuln-watcher")
        await store.agent_heartbeat("slo-tracker")
        await store.agent_heartbeat("drift-detector")
        await store.agent_heartbeat("skill-learner")

        pruned = await prune_stale_agents_and_log(store)

        assert pruned == []
        assert await store.list_events_by_agent("agent-registry") == []
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {
            "vuln-watcher", "slo-tracker", "drift-detector", "skill-learner",
        }

    async def test_no_stale_rows_logs_no_event(self) -> None:
        store = await make_store()
        await store.agent_heartbeat("vuln-watcher")

        pruned = await prune_stale_agents_and_log(store)

        assert pruned == []
        assert await store.list_events_by_agent("agent-registry") == []

    async def test_mixed_stale_and_legitimate_prunes_only_stale(self) -> None:
        """Removed Python agents (incl. former cost/dependency, and
        codechange -- the last Python onboarding agent, removed
        2026-08-07) prune away; only watchers remain."""
        store = await make_store()
        removed = ["chaos", "cicd", "compliance", "hardening", "incident",
                   "infrastructure", "observability", "release", "retirement",
                   "cost", "dependency", "codechange"]
        for name in removed:
            await store.register_agent(name, name)
        await store.agent_heartbeat("vuln-watcher")
        await store.agent_heartbeat("slo-tracker")
        await store.agent_heartbeat("drift-detector")
        await store.agent_heartbeat("skill-learner")

        pruned = await prune_stale_agents_and_log(store)

        assert sorted(pruned) == sorted(removed)
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {
            "vuln-watcher", "slo-tracker", "drift-detector", "skill-learner",
        }


