---
mode: agent
name: codechange
category: codechange
code_ref: agentit.agents.codechange:CodeChangeAgent
resource_tier: large
description: .gitignore, OTel instrumentation, structured logging
---

# Code Change Agent

## What This Agent Does
Patches the onboarded application's *own source repository* (a
`.gitignore`, OpenTelemetry instrumentation, structured logging) rather
than generating a Kubernetes manifest — a fundamentally different
capability skills don't model, which is why this stays a real Python
agent rather than becoming a `mode: detect`/`template` skill — see
`docs/agent-removal-readiness.md`.

## Code Reference
`code_ref` (`agentit.agents.codechange:CodeChangeAgent`) is a
`module:ClassName` string, lazy-imported by
`agents/capabilities.py::get_agent_class()` at the exact call site every
other agent already goes through — this registration file only supplies
*where* the class lives, not a copy of its logic. `CodeChangeAgent`'s own
`.run()` implementation is unchanged by this file's existence.

## Resource Tier
`large` — see `RESOURCE_TIERS` in `agents/capabilities.py` for the actual
CPU/memory request/limit values this tier maps to when run as a
Kubernetes Job (`AGENTIT_AGENT_MODE=kubernetes`). Larger than `cost`/
`dependency` because source-repo patch generation is more compute-intensive.

## Verification
`agentit run-agent codechange --report <assessment.json>` produces at
least one `GeneratedFile`, and
`agentit.agents.capabilities.get_agent_class("codechange")` resolves to
`CodeChangeAgent` — see `tests/test_agent_registration.py`.
