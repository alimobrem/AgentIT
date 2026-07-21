---
mode: agent
name: dependency
category: dependency
code_ref: agentit.agents.dependency:DependencyAgent
resource_tier: small
description: Dependency report, Renovate/Dependabot config
---

# Dependency Agent

## What This Agent Does
Generates a narrative `dependency-report.md` (detected ecosystems, known
CVEs) plus Renovate/Dependabot configuration for the onboarded
application. Kept as a real Python agent (not a `mode: detect`/`template`
skill) specifically for the narrative report, which depends on
runtime-computed data (detected ecosystems/CVEs) a static skill template
has no access to — see `docs/agent-removal-readiness.md`.

## Code Reference
`code_ref` (`agentit.agents.dependency:DependencyAgent`) is a
`module:ClassName` string, lazy-imported by
`agents/capabilities.py::get_agent_class()` at the exact call site every
other agent already goes through — this registration file only supplies
*where* the class lives, not a copy of its logic. `DependencyAgent`'s own
`.run()` implementation is unchanged by this file's existence.

## Resource Tier
`small` — see `RESOURCE_TIERS` in `agents/capabilities.py` for the actual
CPU/memory request/limit values this tier maps to when run as a
Kubernetes Job (`AGENTIT_AGENT_MODE=kubernetes`).

## Verification
`agentit run-agent dependency --report <assessment.json>` produces at
least one `GeneratedFile` (dependency-report.md + Renovate/Dependabot
config), and `agentit.agents.capabilities.get_agent_class("dependency")`
resolves to `DependencyAgent` — see `tests/test_agent_registration.py`.
