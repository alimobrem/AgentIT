---
name: capability-scout
mode: LLM polling
interval: 24 hours
code_ref: agentit.watchers.capability_scout:CapabilityScout
description: Reads fleet usage/effectiveness data and doc-gap signals, proposes one small change to AgentIT itself as a draft PR for human review — requires an LLM connection and GITHUB_TOKEN
---

# Capability Scout

## What This Watcher Does
Reads fleet usage/effectiveness data and doc-gap signals, and proposes
one small, evidence-grounded change to AgentIT itself as a draft PR for
human review — see `watchers/capability_scout.py`. Requires an LLM
connection and `GITHUB_TOKEN`. Never auto-merges — see that module's own
docstring.

## Code Reference
`code_ref` (`agentit.watchers.capability_scout:CapabilityScout`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "capability-scout"` whose `mode`/`interval`/`description` match
this file's frontmatter exactly — see `tests/test_watcher_registration.py`.
