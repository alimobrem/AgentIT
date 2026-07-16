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
    def test_includes_surviving_python_agents(self) -> None:
        known = get_known_agent_names()
        assert "cost" in known
        assert "dependency" in known
        assert "codechange" in known

    def test_includes_long_lived_watchers(self) -> None:
        known = get_known_agent_names()
        assert "vuln-watcher" in known
        assert "slo-tracker" in known
        assert "drift-detector" in known
        assert "skill-learner" in known

    def test_excludes_removed_python_agents(self) -> None:
        """The 9 Python agents removed in favor of skills-only generation
        must never appear in the known-names set, or their stale
        agent_registry rows would never get pruned."""
        known = get_known_agent_names()
        removed = {
            "chaos", "cicd", "compliance", "hardening", "incident",
            "infrastructure", "observability", "release", "retirement",
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
        await store.register_agent("cost", "cost")

        pruned = await prune_stale_agents_and_log(store)

        assert sorted(pruned) == ["chaos", "cicd", "security"]
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"cost"}

        events = await store.list_events_by_agent("agent-registry")
        assert len(events) == 1
        assert events[0]["action"] == "agent-registry-pruned"
        assert events[0]["severity"] == "warning"
        assert "chaos" in events[0]["summary"]

    async def test_preserves_legitimate_agents_and_watchers(self) -> None:
        """The 3 surviving Python agents plus the 4 watchers must never be
        pruned, even if they're the only rows in the registry."""
        store = await make_store()
        await store.register_agent("cost", "cost")
        await store.register_agent("dependency", "dependency")
        await store.register_agent("codechange", "codechange")
        await store.agent_heartbeat("vuln-watcher")
        await store.agent_heartbeat("slo-tracker")
        await store.agent_heartbeat("drift-detector")
        await store.agent_heartbeat("skill-learner")

        pruned = await prune_stale_agents_and_log(store)

        assert pruned == []
        assert await store.list_events_by_agent("agent-registry") == []
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {
            "cost", "dependency", "codechange",
            "vuln-watcher", "slo-tracker", "drift-detector", "skill-learner",
        }

    async def test_no_stale_rows_logs_no_event(self) -> None:
        store = await make_store()
        await store.register_agent("cost", "cost")

        pruned = await prune_stale_agents_and_log(store)

        assert pruned == []
        assert await store.list_events_by_agent("agent-registry") == []

    async def test_mixed_stale_and_legitimate_prunes_only_stale(self) -> None:
        """Regression check for the exact real-world scenario this was
        built for: 9 removed Python agents mixed in the registry alongside
        the 3 surviving agents and the 4 watchers."""
        store = await make_store()
        removed = ["chaos", "cicd", "compliance", "hardening", "incident",
                   "infrastructure", "observability", "release", "retirement"]
        for name in removed:
            await store.register_agent(name, name)
        await store.register_agent("cost", "cost")
        await store.register_agent("dependency", "dependency")
        await store.register_agent("codechange", "codechange")
        await store.agent_heartbeat("vuln-watcher")
        await store.agent_heartbeat("slo-tracker")
        await store.agent_heartbeat("drift-detector")
        await store.agent_heartbeat("skill-learner")

        pruned = await prune_stale_agents_and_log(store)

        assert sorted(pruned) == sorted(removed)
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {
            "cost", "dependency", "codechange",
            "vuln-watcher", "slo-tracker", "drift-detector", "skill-learner",
        }


