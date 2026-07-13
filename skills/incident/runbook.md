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
- The LLM-tailored output is markdown, not YAML — this skill reasons about
  the app's specific architecture rather than emitting a static template
- Reference only components and services detected in the assessment
- Tailor diagnostic commands to the app's actual runtime (e.g., JVM
  flags for Java apps, GOMAXPROCS for Go, connection pool queries for
  PostgreSQL)
- Do not fabricate services or dependencies not present in the codebase

## Template
Deterministic baseline used when no LLM is available: a generic runbook
covering common K8s failure modes (crash loops, OOM kills, image pull
failures) that apply regardless of stack, since no placeholder exists for
"detected databases" or "detected runtime" — only `{{app_name}}` is
substituted. Delivered as a ConfigMap so the skill engine — which only ever
writes a single `.yaml` file per skill — produces a real, applyable K8s
object instead of a bare markdown file. The LLM enhancement adds stack-
specific failure modes and diagnostic commands (e.g., PostgreSQL connection
exhaustion, JVM heap dumps).

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-incident-runbook
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: incident-runbook
data:
  runbook.md: |
    # Incident Response Runbook — {{app_name}}

    ## Quick Reference
    | Symptom | Severity | First Responder Action |
    |---|---|---|
    | Pod CrashLoopBackOff | SEV2 | Check `kubectl logs -l app.kubernetes.io/name={{app_name}} --tail=100 --previous` |
    | OOMKilled (exit 137) | SEV2 | Compare memory limits vs actual usage; raise limits or investigate a leak |
    | ImagePullBackOff | SEV3 | Verify the image reference and pull secret |
    | 5xx error rate spike | SEV1/SEV2 | Check recent deploys; roll back if correlated |
    | Upstream dependency timeout | SEV2 | Check downstream service health and NetworkPolicy rules |

    ## Triage Steps
    1. Confirm the alert source and affected component.
    2. `kubectl get pods -l app.kubernetes.io/name={{app_name}}` — check pod status and restart counts.
    3. `kubectl logs -l app.kubernetes.io/name={{app_name}} --tail=100` — check recent errors.
    4. `kubectl describe pod <pod>` — check events for scheduling/probe failures.
    5. Check dashboards/alerts for correlated metrics (error rate, latency, saturation).

    ## Escalation Matrix
    | Severity | Response Time | Escalate To |
    |---|---|---|
    | SEV1 — full outage | 15 minutes | On-call SRE, then engineering lead |
    | SEV2 — degraded | 30 minutes | On-call SRE |
    | SEV3 — minor/cosmetic | Next business day | Service owner (ticket) |
    | SEV4 — informational | No SLA | Backlog |

    ## Recovery Procedures
    - Rolling restart: `kubectl rollout restart deployment/{{app_name}}`
    - Scale up: `kubectl scale deployment/{{app_name}} --replicas=3`
    - Roll back (Deployment): `kubectl rollout undo deployment/{{app_name}}`
    - Roll back (Argo Rollouts): `kubectl argo rollouts undo {{app_name}}`

    ## Verification
    - After recovery, confirm pods are Running and Ready for at least 10 minutes.
    - Confirm error rate and latency have returned to baseline.
    - File a post-incident review within 48 hours for SEV1/SEV2 incidents.
```
