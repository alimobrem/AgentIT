---
name: otel-collector
domain: observability
version: 2
# Not the SOLUTION_CONTRACT for `tracing` — app SDK instrumentation is
# detect-only (aligns with `instrumentation`). This skill remains available
# for operators who explicitly want a collector, but Scan will not open it
# as a clear for "No distributed tracing detected".
triggers:
  - opentelemetry-collector
  - otel-collector
  - tempo-exporter
outputs:
  - OpenTelemetryCollector
property: "Application traces are collected and exported"
mode: template
---

# OpenTelemetry Collector

## Property
An OpenTelemetry Collector sidecar receives traces from the application
via OTLP (gRPC and HTTP) and exports them to Tempo for distributed
trace storage and querying.

## Constraints
- Sidecar mode — one collector per pod, no shared collector deployment
- OTLP gRPC receiver on port 4317, OTLP HTTP receiver on port 4318
- Export to Tempo via OTLP gRPC (tempo-distributor default endpoint)
- Requires the OpenTelemetry Operator CRD on the cluster

## Template

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: {{app_name}}-otel
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  mode: sidecar
  config:
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318
    processors:
      batch:
        timeout: 5s
        send_batch_size: 1024
      memory_limiter:
        check_interval: 1s
        limit_mib: 256
        spike_limit_mib: 64
    exporters:
      otlp:
        endpoint: tempo-distributor.tracing.svc.cluster.local:4317
        tls:
          insecure: true
    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [otlp]
```

## Verification
- `oc get opentelemetrycollectors` shows the collector in Ready state
- Application pods have the collector sidecar container
- Traces appear in Tempo/Jaeger UI for the application's service name
