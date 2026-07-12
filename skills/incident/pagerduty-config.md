---
name: pagerduty-config
domain: incident
version: 1
triggers:
  - pagerduty
  - alert
  - oncall
  - notification
outputs:
  - ConfigMap
property: "Alerts route to the on-call team via PagerDuty"
mode: template
---

# PagerDuty Configuration

## Property
Critical and high-severity alerts route to the on-call team via PagerDuty
integration, ensuring incidents are acknowledged and acted on within
defined SLA windows.

## Constraints
- ConfigMap holds the PagerDuty routing configuration
- Integration key is referenced from a Secret, never hardcoded
- Routing rules map severity labels to PagerDuty escalation policies
- Critical alerts trigger immediate page; warning alerts create incidents

## Template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-pagerduty-config
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: incident-management
data:
  pagerduty.yaml: |
    global:
      integration_key_secret: {{app_name}}-pagerduty-key
      integration_key_key: PAGERDUTY_INTEGRATION_KEY
    routing:
      - match:
          severity: critical
        action: trigger
        escalation_policy: default
        urgency: high
        description_template: "[CRITICAL] {{app_name}} - {{ .AlertName }}: {{ .Summary }}"
      - match:
          severity: warning
        action: trigger
        escalation_policy: default
        urgency: low
        description_template: "[WARNING] {{app_name}} - {{ .AlertName }}: {{ .Summary }}"
    resolve_timeout: 5m
    dedup_key_template: "{{app_name}}/{{ .AlertName }}/{{ .Namespace }}"
```

## Verification
- ConfigMap exists with correct routing rules
- Secret `{{app_name}}-pagerduty-key` is created on-cluster (not in repo)
- Critical alerts produce PagerDuty incidents with high urgency
- Warning alerts produce PagerDuty incidents with low urgency
