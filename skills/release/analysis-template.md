---
name: analysis-template
domain: release
version: 1
triggers:
  - analysis
  - canary
  - verification
  - release
  - rollout
outputs:
  - AnalysisTemplate
property: "Canary deployments are verified by automated analysis"
mode: template
---

# Argo Analysis Template — Canary Verification

## Property
Canary deployments are gated by automated analysis that checks
success-rate >= 95% and error-rate <= 5% before promoting traffic,
preventing bad releases from reaching production.

## Constraints
- Uses argoproj.io/v1alpha1 AnalysisTemplate CRD
- Prometheus metrics provider queries the app's HTTP metrics
- Two metrics: success-rate (must be >= 0.95) and error-rate (must be <= 0.05)
- Analysis runs for 5 minutes with 60-second measurement intervals
- Failure count threshold of 3 — tolerates transient blips

## Template

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: {{app_name}}-canary-analysis
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: release-analysis
spec:
  args:
    - name: service-name
      value: {{app_name}}
  metrics:
    - name: success-rate
      interval: 60s
      count: 5
      failureLimit: 3
      successCondition: result[0] >= 0.95
      provider:
        prometheus:
          address: http://prometheus.monitoring.svc:9090
          query: |
            sum(rate(http_requests_total{app="{{`{{args.service-name}}`}}",status=~"2.."}[2m]))
            / sum(rate(http_requests_total{app="{{`{{args.service-name}}`}}"}[2m]))
    - name: error-rate
      interval: 60s
      count: 5
      failureLimit: 3
      successCondition: result[0] <= 0.05
      provider:
        prometheus:
          address: http://prometheus.monitoring.svc:9090
          query: |
            sum(rate(http_requests_total{app="{{`{{args.service-name}}`}}",status=~"5.."}[2m]))
            / sum(rate(http_requests_total{app="{{`{{args.service-name}}`}}"}[2m]))
```

## Verification
- kubectl get analysistemplate {{app_name}}-canary-analysis — template exists
- AnalysisRun created during rollout shows success-rate and error-rate results
- Rollout aborts if either metric fails 3 consecutive measurements
- Prometheus queries return data for the app's HTTP metrics
