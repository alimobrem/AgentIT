# Proposal: Add failure alerting / retry logic for the remediation-loop agent

> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md

**Risk:** low

## Gap

The remediation-loop agent has a 0 % success rate across 48 total events (6 explicit failures, 42 events with neither a success nor a failure recorded), all occurring within a ~5-hour window on 2026-07-15. No other agent shows this failure pattern. There is currently no evidence of any circuit-breaker, retry, or alerting path in AgentIT's own code that would surface or recover from a sustained run of agent failures.

## Evidence

{"agent": "remediation-loop", "total_events": 48, "successes": 0, "failures": 6, "success_rate": 0.0, "first_seen": "2026-07-15T14:32:22.426477+00:00", "last_seen": "2026-07-15T19:22:39.576059+00:00"}

## Suggested target files

- `src/agentit/agents/remediation_loop.py`
- `src/agentit/monitoring/agent_health.py`
- `tests/test_remediation_loop_health.py`

## Suggested change

1. In src/agentit/monitoring/agent_health.py, add a small helper function `check_agent_failure_threshold(agent_stats, agent_name, min_events=10, max_failure_rate=0.5)` that returns True when an agent's failure rate exceeds the threshold over a meaningful sample. 2. In src/agentit/agents/remediation_loop.py, call this helper after each run cycle and log a structured WARNING (or raise a recoverable exception that the caller can catch) when the threshold is breached, so operators are alerted and the loop can back off. Keep changes minimal: no refactor of existing logic, only add the threshold check call and the log/raise.

## Test plan

tests/test_remediation_loop_health.py should assert: (a) check_agent_failure_threshold returns True when given stats matching remediation-loop's observed pattern (48 events, 0 successes, 6 failures); (b) returns False for a healthy agent like slo-tracker (541 events, 541 successes); (c) returns False when total_events is below min_events (sparse data should not trigger alert); (d) that the remediation-loop run method emits a WARNING log entry when the threshold function returns True.
