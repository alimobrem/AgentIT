---
name: runbook
domain: incident
version: 1
triggers:
  - runbook
  - incident
  - operations
  - troubleshoot
  - oncall
outputs:
  - Runbook
property: "Operations team has a runbook for common failure modes"
mode: llm
---

# Incident Runbook

## Property
The operations team has a comprehensive runbook documenting common failure
modes, triage steps, escalation paths, and recovery procedures tailored
to the application's specific stack and deployment topology.

## Instructions
Generate a markdown runbook for the assessed application. The document must:

1. **Catalog common failure modes** based on the application's stack —
   pod crash loops, OOM kills, database connection exhaustion, upstream
   dependency timeouts, certificate expiry, storage pressure, and any
   stack-specific failures detected in the assessment.

2. **Provide triage steps** for each failure mode — what to check first,
   which logs to inspect, key metrics to look at, and diagnostic commands
   (kubectl, oc, curl, psql, etc.) relevant to the app's runtime.

3. **Define escalation paths** — severity levels (SEV1–SEV4), response
   time targets, who to page, and when to escalate from on-call to
   engineering leads.

4. **Document recovery procedures** — step-by-step actions to restore
   service for each failure mode, including rollback commands, scaling
   actions, cache invalidation, and data recovery steps.

5. **Use this structure:**
   - Header with app name, team, last-updated date
   - Quick reference table: Symptom | Severity | First Responder Action
   - Detailed runbook sections per failure mode
   - Escalation matrix
   - Post-incident review checklist

## Constraints
- Output is markdown, not YAML — this skill reasons about the app's
  specific architecture rather than emitting a static template
- Reference only components and services detected in the assessment
- Tailor diagnostic commands to the app's actual runtime (e.g., JVM
  flags for Java apps, GOMAXPROCS for Go, connection pool queries for
  PostgreSQL)
- Do not fabricate services or dependencies not present in the codebase
