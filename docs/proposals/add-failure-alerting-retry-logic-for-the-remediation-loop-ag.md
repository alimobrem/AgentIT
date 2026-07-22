# Proposal: Add failure alerting / retry logic for the remediation-loop agent

> Proposed by AgentIT's capability-scout — see docs/history/self-improvement-for-agentit.md

**Risk:** low

## Gap

The remediation-loop agent has a 0% success rate across 48 total events with 6 recorded failures and no successes, yet AgentIT's own codebase appears to have no mechanism that detects this sustained failure pattern and surfaces it or attempts a retry. Every other high-volume agent (slo-tracker, drift-detector, vuln-watcher) is at 100% success, making remediation-loop an outlier that is silently failing.

## Evidence

From agent_stats: {"agent": "remediation-loop", "total_events": 48, "successes": 0, "failures": 6, "success_rate": 0.0, "first_seen": "2026-07-15T14:32:22.426477+00:00", "last_seen": "2026-07-15T19:22:39.576059+00:00"}. No doc_gaps, no tick_failures, and no check_compliance entries reference this agent, confirming the failure is undetected by existing monitoring paths.

## Suggested target files

- `src/agentit/agents/remediation_loop.py`
- `checks/remediation_loop_health.py`
- `tests/test_remediation_loop_health.py`

## Suggested change

1. In checks/remediation_loop_health.py, add a small check function that queries agent_stats for remediation-loop and raises a structured alert (log + optional event emission) when success_rate == 0 and total_events > a configurable threshold (default 10). 2. In src/agentit/agents/remediation_loop.py, wrap the main execution path in a try/except that catches the top-level exception, increments a local retry counter (max 3), waits with exponential back-off, and re-raises after exhausting retries so the failure is still recorded. Keep changes strictly inside the remediation-loop agent file; do not touch shared infrastructure. 3. Wire the new check into the existing checks runner if one exists, otherwise call it standalone.

## Test plan

tests/test_remediation_loop_health.py should assert: (a) check passes (no alert) when success_rate > 0 or total_events <= threshold; (b) check raises/returns an alert object when success_rate == 0.0 and total_events == 48 (matching the observed data); (c) the retry wrapper in remediation_loop.py retries exactly 3 times before re-raising, verified by mocking the inner execution function to always raise and asserting it was called 3 times; (d) a successful inner call on the second attempt results in no re-raise and increments the success counter.
