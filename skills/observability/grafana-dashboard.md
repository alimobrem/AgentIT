---
name: grafana-dashboard
domain: observability
version: 1
triggers:
  - dashboard
  - grafana
  - visualization
  - metrics
outputs:
  - ConfigMap
property: "Application has a Grafana dashboard for RED metrics"
mode: template
---

# Grafana Dashboard

## Property
A Grafana dashboard visualizes the application's RED metrics
(Rate, Errors, Duration) plus pod restarts, giving operators
a single pane of glass for service health.

## Constraints
- ConfigMap must carry the `grafana_dashboard: "1"` label for sidecar auto-discovery
- Dashboard JSON includes 4 panels: request rate, error rate, p99 latency, pod restarts
- Uses monitoring.coreos.com PromQL conventions matching ServiceMonitor metrics
- Datasource uid set to `prometheus` (default Grafana datasource name)

## Template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-grafana-dashboard
  labels:
    app.kubernetes.io/name: {{app_name}}
    grafana_dashboard: "1"
data:
  {{app_name}}-dashboard.json: |
    {
      "annotations": { "list": [] },
      "editable": true,
      "fiscalYearStartMonth": 0,
      "graphTooltip": 1,
      "links": [],
      "panels": [
        {
          "title": "Request Rate",
          "type": "timeseries",
          "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
          "targets": [
            {
              "expr": "sum(rate(http_requests_total{app=\"{{app_name}}\"}[5m]))",
              "legendFormat": "req/s"
            }
          ]
        },
        {
          "title": "Error Rate",
          "type": "timeseries",
          "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
          "targets": [
            {
              "expr": "sum(rate(http_requests_total{app=\"{{app_name}}\",status=~\"5..\"}[5m])) / sum(rate(http_requests_total{app=\"{{app_name}}\"}[5m]))",
              "legendFormat": "error %"
            }
          ]
        },
        {
          "title": "P99 Latency",
          "type": "timeseries",
          "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
          "targets": [
            {
              "expr": "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{app=\"{{app_name}}\"}[5m])) by (le))",
              "legendFormat": "p99 (s)"
            }
          ]
        },
        {
          "title": "Pod Restarts",
          "type": "timeseries",
          "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
          "targets": [
            {
              "expr": "increase(kube_pod_container_status_restarts_total{pod=~\"{{app_name}}.*\"}[1h])",
              "legendFormat": "restarts"
            }
          ]
        }
      ],
      "schemaVersion": 39,
      "tags": ["{{app_name}}", "red-metrics"],
      "templating": { "list": [] },
      "time": { "from": "now-1h", "to": "now" },
      "title": "{{app_name}} RED Metrics",
      "uid": "{{app_name}}-red"
    }
```

## Verification
- Grafana dashboards list shows "{{app_name}} RED Metrics"
- All 4 panels render data when the application is receiving traffic
