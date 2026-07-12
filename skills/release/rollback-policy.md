---
name: rollback-policy
domain: release
version: 1
triggers:
  - rollback
  - policy
  - recovery
  - revert
outputs:
  - ConfigMap
property: "Rollback procedures are documented and automated"
mode: template
---

# Rollback Policy

## Property
Rollback procedures are documented in a ConfigMap with specific commands,
error budget thresholds that trigger automatic rollback, and step-by-step
recovery instructions for the operations team.

## Constraints
- ConfigMap stores rollback policy as structured YAML data
- Error budget thresholds define when rollback is mandatory
- Includes Argo Rollouts, Argo CD, and kubectl rollback commands
- Documents both automated and manual rollback paths

## Template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-rollback-policy
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: release-policy
data:
  rollback-policy.yaml: |
    application: {{app_name}}
    version: "1.0"

    error_budget:
      window: 30d
      target_availability: 99.9
      remaining_threshold_pct: 25
      action_on_breach: automatic_rollback

    thresholds:
      error_rate_5m: 0.05
      p99_latency_5m: 2.0s
      pod_restart_count_10m: 3
      oom_kill_count_1h: 1

    automated_rollback:
      argo_rollouts: |
        kubectl argo rollouts undo {{app_name}}
      argo_cd: |
        argocd app rollback {{app_name}}
      kubectl: |
        kubectl rollout undo deployment/{{app_name}}

    manual_rollback:
      steps:
        - "1. Verify the issue: kubectl argo rollouts get rollout {{app_name}}"
        - "2. Abort current rollout: kubectl argo rollouts abort {{app_name}}"
        - "3. Undo to previous revision: kubectl argo rollouts undo {{app_name}}"
        - "4. Verify stable revision is serving: kubectl argo rollouts status {{app_name}}"
        - "5. Check pod health: kubectl get pods -l app.kubernetes.io/name={{app_name}}"
        - "6. Validate metrics: curl -s http://prometheus:9090/api/v1/query?query=up{app='{{app_name}}'}"
        - "7. Notify team in Slack #{{app_name}}-releases"
        - "8. Create post-incident issue"

    escalation:
      - condition: "rollback fails or pods not healthy after 5m"
        action: "page on-call SRE via PagerDuty"
      - condition: "data corruption suspected"
        action: "escalate to engineering lead immediately"
```

## Verification
- ConfigMap exists with rollback commands and thresholds
- Error budget thresholds match the team's SLO targets
- Rollback commands reference the correct app name
- Manual steps are executable by on-call engineers
