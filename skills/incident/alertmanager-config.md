---
name: alertmanager-config
domain: incident
version: 1
triggers:
  - alertmanager
  - alert
  - routing
  - notification
outputs:
  - ConfigMap
property: "Alert routing is configured for severity-based escalation"
mode: template
---

# AlertManager Configuration

## Property
AlertManager routes alerts by severity: critical alerts page the on-call
team via PagerDuty, warnings go to the team Slack channel, and
informational alerts are delivered by email.

## Constraints
- ConfigMap contains the AlertManager routing configuration
- Three receiver tiers: PagerDuty (critical), Slack (warning), email (info)
- Credentials referenced from Secrets, never hardcoded in the ConfigMap
- Group-by labels prevent alert storms from flooding receivers
- Repeat interval prevents duplicate notifications within a window

## Template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-alertmanager-config
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: alerting
data:
  alertmanager.yaml: |
    global:
      resolve_timeout: 5m
    route:
      receiver: default-email
      group_by:
        - alertname
        - namespace
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
      routes:
        - match:
            severity: critical
          receiver: pagerduty-critical
          group_wait: 10s
          repeat_interval: 1h
          continue: false
        - match:
            severity: warning
          receiver: slack-warnings
          repeat_interval: 4h
          continue: false
        - match:
            severity: info
          receiver: default-email
          repeat_interval: 12h
    receivers:
      - name: pagerduty-critical
        pagerduty_configs:
          - service_key_file: /etc/alertmanager/secrets/pagerduty-key
            description: "{{ .GroupLabels.alertname }} in {{ .GroupLabels.namespace }}"
            severity: "{{ .CommonLabels.severity }}"
      - name: slack-warnings
        slack_configs:
          - api_url_file: /etc/alertmanager/secrets/slack-webhook-url
            channel: "#{{app_name}}-alerts"
            title: "[{{ .Status | toUpper }}] {{ .GroupLabels.alertname }}"
            text: "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ end }}"
            send_resolved: true
      - name: default-email
        email_configs:
          - to: "{{app_name}}-team@example.com"
            from: "alertmanager@example.com"
            smarthost: "smtp.example.com:587"
            send_resolved: true
```

## Verification
- ConfigMap exists with three receiver tiers
- Critical alerts route to PagerDuty, not Slack or email
- Warning alerts route to Slack channel
- Info alerts route to email
- Secrets for PagerDuty key and Slack webhook are created on-cluster
