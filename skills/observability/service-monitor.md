---
name: service-monitor
domain: observability
version: 1
triggers:
  - metrics
  - monitoring
  - prometheus
  - observability
outputs:
  - ServiceMonitor
property: "Application metrics are collected by Prometheus"
mode: template
---

# Service Monitor

## Property
Prometheus automatically discovers and scrapes the application's
metrics endpoint, making RED metrics (Rate, Errors, Duration)
available for dashboards and alerting.

## Constraints
- Scrape /metrics on the app's HTTP port (default 8080)
- Interval: 30s (balances freshness vs load)
- Use monitoring.coreos.com/v1 API
- Only generate if the cluster has the Prometheus operator CRD
- `spec.selector.matchLabels` must match a **live** Service (HPA pattern).
  Clear-evidence `selector_target` refuses empty / zero-match selectors

## Template

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {{app_name}}-monitor
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  selector:
    matchLabels:
      app: {{app_name}}
  endpoints:
    - port: http
      path: /metrics
      interval: 30s
```

## Verification
- Prometheus targets page shows the app as UP
- PromQL query returns data: up{job="{{app_name}}"}
- Clear-evidence refuses ServiceMonitor with empty or non-matching selector
