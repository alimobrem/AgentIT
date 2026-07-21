---
name: reassess-scheduler
mode: Polling
interval: 1 hour
code_ref: agentit.watchers.reassess_scheduler:ReassessScheduler
description: Checks every app's configured re-assessment cadence (daily/weekly/monthly, set on its Assessment Detail page) and automatically re-Assesses any app that's due, via the same route the manual Scan button uses
---

# Reassess Scheduler

## What This Watcher Does
Checks every app's configured re-assessment cadence (daily/weekly/monthly,
set on its Assessment Detail page) and automatically re-Assesses any app
that's due, via the same route the manual Scan button uses — see
`watchers/reassess_scheduler.py`.

## Code Reference
`code_ref` (`agentit.watchers.reassess_scheduler:ReassessScheduler`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "reassess-scheduler"` whose `mode`/`interval`/`description`
match this file's frontmatter exactly — see
`tests/test_watcher_registration.py`.
