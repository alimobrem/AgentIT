---
mode: agent
name: codechange
category: codechange
code_ref: agentit.agents.codechange:CodeChangeAgent
resource_tier: large
description: Optional source patches — .gitignore, OTel, logging, Dockerfile/health
---

# Code Change Agent (optional source-patch path)

## What This Agent Does
**Not a peer domain agent to skills.** Skills own cluster remediations
(VPA, NetworkPolicy, Tekton, Renovate configs, …). This agent only
proposes patches to the onboarded application's *own source repository*
(`.gitignore`, OpenTelemetry instrumentation, structured logging,
Dockerfile/health) — a capability skills don't model. It runs optionally
when criticality is high/critical or overall score is low. See
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
Kubernetes Job (`AGENTIT_AGENT_MODE=kubernetes`). Source-repo patch
generation is more compute-intensive than template skill rendering.

## Verification
`agentit run-agent codechange --report <assessment.json>` produces at
least one `GeneratedFile`, and
`agentit.agents.capabilities.get_agent_class("codechange")` resolves to
`CodeChangeAgent` — see `tests/test_agent_registration.py`.
