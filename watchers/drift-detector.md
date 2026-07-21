---
name: drift-detector
mode: Argo CD polling
interval: 10 minutes
code_ref: agentit.watchers.drift_detector:DriftDetector
description: Queries Argo CD apps for OutOfSync state and auto-syncs them back to the Git-declared state
---

# Drift Detector

## What This Watcher Does
Queries Argo CD for apps in `OutOfSync` state and auto-syncs them back to
the Git-declared state — see `watchers/drift_detector.py`. Also deprecates
skills whose output API kind has been removed from the cluster (see
`skill_engine.py`'s deprecation lifecycle).

## Code Reference
`code_ref` (`agentit.watchers.drift_detector:DriftDetector`) is a
`module:ClassName` string recorded here for registration purposes only —
`watchers/__init__.py`'s registration/heartbeat wiring (`record_tick`,
`sleep_with_heartbeat`) is unchanged by this file's existence; only the
*listing* of which watchers exist moved from a Python list literal
(`agents/capabilities.py`'s old `WATCHER_AGENTS`) to this Markdown file.

## Verification
`agentit.agents.capabilities.WATCHER_AGENTS` includes an entry with
`name == "drift-detector"` whose `mode`/`interval`/`description` match
this file's frontmatter exactly — see `tests/test_watcher_registration.py`.
