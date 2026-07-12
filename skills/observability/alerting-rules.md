---
name: alerting-rules
domain: observability
version: 1
triggers:
  - alert
  - monitoring
  - observability
  - prometheus
outputs:
  - PrometheusRule
property: "Application has alerting rules for error rate, latency, and pod health"
mode: template
---

# Alerting Rules

## Property
Prometheus alerts fire when the application's error rate exceeds 5%,
p99 latency exceeds 1 second, or pods crash-loop more than 3 times
in 10 minutes.

## Template

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: {{app_name}}-alerts
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  groups:
    - name: {{app_name}}.rules
      rules:
        - alert: HighErrorRate
          expr: |
            sum(rate(http_requests_total{app="{{app_name}}",status=~"5.."}[5m]))
            / sum(rate(http_requests_total{app="{{app_name}}"}[5m])) > 0.05
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "Error rate above 5% for {{app_name}}"
        - alert: HighLatency
          expr: |
            histogram_quantile(0.99,
              sum(rate(http_request_duration_seconds_bucket{app="{{app_name}}"}[5m])) by (le)
            ) > 1
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "P99 latency above 1s for {{app_name}}"
        - alert: PodCrashLooping
          expr: |
            increase(kube_pod_container_status_restarts_total{pod=~"{{app_name}}.*"}[10m]) > 3
          for: 1m
          labels:
            severity: critical
          annotations:
            summary: "Pod crash looping for {{app_name}}"
```
