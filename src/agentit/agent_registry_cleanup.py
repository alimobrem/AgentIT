"""Prune stale `agent_registry` rows for agents no longer known to the codebase.

Companion to `skill_inventory.py`: that module tracks additions/removals to
the skills/checks catalog and logs `skill-added`/`skill-removed` events; this
module tracks the analogous problem for the `agent_registry` table and logs
a matching `agent-registry-pruned` event.

When a Python agent class is permanently removed from the codebase (e.g. the
skills-only migration that removed `chaos`, `cicd`, `compliance`,
`hardening`, `incident`, `infrastructure`, `observability`, `release`, and
`retirement` in favor of skills-only generation), nothing can ever call
`register_agent()`/`agent_heartbeat()` for it again -- both methods only
ever insert or refresh a row, never remove one -- so its last-registered row
sits in `agent_registry` forever, reported as `status: active` with a
`last_heartbeat` frozen at whenever it last ran.

The known-agent-names source of truth is `agents/capabilities.py`: the 3
surviving Python onboarding agents (`AGENT_CLASSES`) plus the 4 long-lived
watchers (`WATCHER_AGENTS`) that heartbeat directly, bypassing
`register_agent()` entirely.
"""
from __future__ import annotations

from agentit.agents.capabilities import AGENT_CLASSES, WATCHER_AGENTS


def get_known_agent_names() -> frozenset[str]:
    """Every agent name the running codebase can currently register or
    heartbeat for. Any `agent_registry` row outside this set belongs to an
    agent that has since been removed from the codebase."""
    return frozenset(AGENT_CLASSES) | frozenset(w["name"] for w in WATCHER_AGENTS)


def _pruned_event(pruned: list[str]) -> dict:
    """Pure helper for the event shape below."""
    return dict(
        agent_id="agent-registry", action="agent-registry-pruned", target_app=None,
        severity="warning",
        summary=(
            f"Pruned {len(pruned)} stale agent registration(s) no longer in "
            f"codebase: {', '.join(pruned)}"
        ),
    )


async def prune_stale_agents_and_log(store) -> list[str]:
    """Delete `agent_registry` rows for names outside `get_known_agent_names()`
    and log a matching `agent-registry-pruned` event, mirroring
    `skill_inventory.py`'s `skill-added`/`skill-removed` event convention so
    this shows up on the Events page for free. Runs alongside
    `diff_and_log_inventory_changes()` in the portal's hourly background
    maintenance loop.

    Returns the sorted list of pruned agent names (empty if nothing was
    stale).
    """
    pruned = await store.prune_stale_agents(get_known_agent_names())
    if pruned:
        await store.log_event(**_pruned_event(pruned))
    return pruned
