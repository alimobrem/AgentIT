---
name: slo-tracker
mode: Polling
interval: 5 minutes
code_ref: agentit.watchers.slo_tracker:SloTracker
description: Checks SLO status across all assessments, publishes breach alerts, recommends rollbacks
---

# SLO Tracker

## What This Watcher Does
Checks SLO status across all assessments, publishes breach alerts, and
recommends rollbacks — see `watchers/slo_tracker.py`.

## Code Reference
`code_ref` (`agentit.watchers.slo_tracker:SloTracker`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "slo-tracker"` whose `mode`/`interval`/`description` match this
file's frontmatter exactly — see `tests/test_watcher_registration.py`.
