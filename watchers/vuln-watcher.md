---
name: vuln-watcher
mode: Kafka consumer + polling
interval: 6 hours
code_ref: agentit.watchers.vuln_watcher:VulnWatcher
description: Monitors fleet for critical/high findings and raises an alert for each one
---

# Vuln Watcher

## What This Watcher Does
Monitors the fleet for critical/high-severity findings and raises an
alert event for each one it finds — see `watchers/vuln_watcher.py`.

## Code Reference
`code_ref` (`agentit.watchers.vuln_watcher:VulnWatcher`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "vuln-watcher"` whose `mode`/`interval`/`description` match this
file's frontmatter exactly — see `tests/test_watcher_registration.py`.
