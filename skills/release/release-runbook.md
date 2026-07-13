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
- The LLM-tailored output is markdown, not YAML — this skill reasons about
  the app's specific deployment topology rather than emitting a static
  template
- Reference only deployment methods and tools detected in the assessment
- Tailor verification steps to the app's actual health endpoints and
  metrics (not generic examples)
- Do not assume tools or services not present in the codebase

## Template
Deterministic baseline used when no LLM is available: a generic runbook
covering the same five sections listed above with conservative, widely-
applicable thresholds (5% error rate, 2s p99) instead of app-specific
metrics. Delivered as a ConfigMap so the skill engine — which only ever
writes a single `.yaml` file per skill — produces a real, applyable K8s
object instead of a bare markdown file. The LLM enhancement replaces the
generic thresholds and commands with ones tailored to the app's actual
deployment method (Argo Rollouts canary vs. plain Deployment) and metrics.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-release-runbook
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: release-runbook
data:
  release-runbook.md: |
    # Release Runbook — {{app_name}}

    ## Pre-Deployment Checklist
    - [ ] CI pipeline green (build, test, scan, SBOM)
    - [ ] Image scanned with no unresolved Critical/High CVEs
    - [ ] Changelog reviewed
    - [ ] Database migrations tested against a staging copy (if applicable)
    - [ ] Feature flags configured for the new release
    - [ ] Dependent services / on-call notified of the release window

    ## Rollout Procedure
    1. Verify the target image tag/digest matches the reviewed build.
    2. Trigger the deployment via Argo CD sync, or `kubectl argo rollouts`
       (canary) if this app uses Argo Rollouts.
    3. Monitor progress: `kubectl argo rollouts get rollout {{app_name}} --watch`,
       or `kubectl rollout status deployment/{{app_name}}` for a plain Deployment.
    4. Watch canary analysis / metrics at each traffic-weight step.

    ## Post-Deployment Verification
    - [ ] Health endpoint returns 200
    - [ ] Error rate stable (below 5%) for at least 10 minutes after full rollout
    - [ ] p99 latency within normal bounds
    - [ ] No new pod restarts or OOM kills
    - [ ] Smoke test key user journeys

    ## Rollback Triggers
    Roll back immediately if any of the following occur:
    - Error rate above 5%
    - p99 latency above 2 seconds
    - Pod crash-loop or repeated OOM kills
    - Failed smoke test

    Rollback commands:
    - Abort canary: `kubectl argo rollouts abort {{app_name}}`
    - Undo to previous revision: `kubectl argo rollouts undo {{app_name}}`
    - Plain Deployment rollback: `kubectl rollout undo deployment/{{app_name}}`

    ## Communication Plan
    - Notify before/during/after: on-call SRE, service owner, dependent-service owners
    - Channels: team Slack channel, PagerDuty (if severity warrants escalation)
```
