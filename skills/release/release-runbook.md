---
name: release-runbook
domain: release
version: 1
triggers:
  - release
  - deploy
  - runbook
  - checklist
outputs:
  - ReleaseRunbook
property: "Release process is documented with pre/post checks"
mode: llm
---

# Release Runbook

## Property
The release process is documented with a comprehensive runbook covering
pre-deployment validation, rollout monitoring, post-deployment verification,
and rollback trigger conditions specific to the application's stack.

## Instructions
Generate a markdown release runbook for the assessed application. The
document must:

1. **Pre-deployment checklist** — verify CI pipeline green, image scanned,
   changelogs reviewed, database migrations tested, feature flags configured,
   and dependent services notified. Tailor checks to the app's actual
   dependencies detected in the assessment.

2. **Rollout procedure** — step-by-step deployment instructions using the
   app's deployment method (Argo Rollouts canary, Argo CD sync, or plain
   kubectl). Include commands to monitor rollout progress and canary metrics.

3. **Post-deployment verification** — health check endpoints to hit, key
   metrics to watch (error rate, latency, saturation), smoke test commands,
   and how long to observe before declaring the release stable.

4. **Rollback triggers** — specific conditions that mandate rollback
   (error rate > 5%, p99 > 2s, pod crash loops, OOM kills). Include
   exact rollback commands for the app's deployment method.

5. **Communication plan** — who to notify before, during, and after the
   release (Slack channels, PagerDuty, stakeholders).

6. **Use this structure:**
   - Header with app name, release version placeholder, date
   - Pre-deployment checklist (checkbox format)
   - Deployment steps with monitoring commands
   - Post-deployment verification checklist
   - Rollback decision tree
   - Communication template

## Constraints
- Output is markdown, not YAML — this skill reasons about the app's
  specific deployment topology rather than emitting a static template
- Reference only deployment methods and tools detected in the assessment
- Tailor verification steps to the app's actual health endpoints and
  metrics (not generic examples)
- Do not assume tools or services not present in the codebase
