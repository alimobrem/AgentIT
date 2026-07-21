---
mode: agent
name: cost
category: cost
code_ref: agentit.agents.cost:CostOptimizationAgent
resource_tier: small
description: VPA, cost labels, cost report
---

# Cost Optimization Agent

## What This Agent Does
Generates a VerticalPodAutoscaler, cost-allocation labels, and a
narrative `cost-report.md` for the onboarded application. Kept as a real
Python agent (not a `mode: detect`/`template` skill) specifically for the
narrative report, which depends on runtime-computed data (the app's
detected cost tier) a static skill template has no access to — see
`docs/agent-removal-readiness.md`.

## Code Reference
`code_ref` (`agentit.agents.cost:CostOptimizationAgent`) is a
`module:ClassName` string, lazy-imported by
`agents/capabilities.py::get_agent_class()` at the exact call site every
other agent already goes through — this registration file only supplies
*where* the class lives, not a copy of its logic. `CostOptimizationAgent`'s
own `.run()` implementation is unchanged by this file's existence.

## Resource Tier
`small` — see `RESOURCE_TIERS` in `agents/capabilities.py` for the actual
CPU/memory request/limit values this tier maps to when run as a
Kubernetes Job (`AGENTIT_AGENT_MODE=kubernetes`).

## Verification
`agentit run-agent cost --report <assessment.json>` produces at least one
`GeneratedFile` (VPA manifest + cost-report.md), and
`agentit.agents.capabilities.get_agent_class("cost")` resolves to
`CostOptimizationAgent` — see `tests/test_agent_registration.py`.
