---
name: self-health-check
mode: Kube + GitHub API polling
interval: 15 minutes
code_ref: agentit.watchers.self_health_check:SelfHealthCheck
description: Verifies AgentIT's own critical infrastructure end to end -- GitHub webhook delivery health, CI pipeline stall detection, maintenance CronJob success, and cleanup-CronJob effectiveness -- publishing pass/fail events surfaced on the Health page's Self-Health panel and the sitewide Events badge
---

# Self Health Check

## What This Watcher Does
Verifies AgentIT's own critical infrastructure end to end — GitHub
webhook delivery health, CI pipeline stall detection, maintenance CronJob
success, and cleanup-CronJob effectiveness — publishing pass/fail events
surfaced on the Health page's Self-Health panel and the sitewide Events
badge. See `watchers/self_health_check.py`.

Added to `WATCHER_AGENTS` after
docs/extension-model-unification-plan-2026-07-18.md was written (that
doc's "what actually exists today" table lists only 6 watchers) — this
file reflects the real, current 7-watcher list re-verified directly
against `agents/capabilities.py` before this Phase 3 port, per that
plan's own explicit instruction to re-check rather than trust the doc.

## Code Reference
`code_ref` (`agentit.watchers.self_health_check:SelfHealthCheck`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "self-health-check"` whose `mode`/`interval`/`description`
match this file's frontmatter exactly — see
`tests/test_watcher_registration.py`.
