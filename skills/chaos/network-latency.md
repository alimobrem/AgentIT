---
name: network-latency
domain: chaos
version: 1
triggers:
  - chaos
  - resilience
  - resiliency
  - latency
  - network
outputs:
  - ChaosEngine
property: "Application stays responsive under injected network latency"
mode: template
---

# Network Latency — Resiliency Experiment

## Property
The application's health endpoint continues to respond successfully
while 500ms of network latency is injected into a percentage of its
pods, verifying the app degrades gracefully rather than timing out
or crash-looping under slow-network conditions.

## Constraints
- Uses the LitmusChaos generic experiment `pod-network-latency`
- Injects a fixed 500ms latency for 60s against a bounded percentage of pods
- An `httpProbe` continuously checks the app's health endpoint during the
  experiment — the LLM enhancement should replace the generic `/healthz`
  path below with the app's actual detected health endpoint

## Template

```yaml
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: {{app_name}}-network-latency
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  appinfo:
    appns: {{app_name}}
    applabel: app.kubernetes.io/name={{app_name}}
    appkind: deployment
  engineState: active
  chaosServiceAccount: {{app_name}}-chaos-sa
  experiments:
    - name: pod-network-latency
      spec:
        components:
          env:
            - name: TOTAL_CHAOS_DURATION
              value: "60"
            - name: NETWORK_LATENCY
              value: "500"
            - name: PODS_AFFECTED_PERC
              value: "50"
            - name: NETWORK_INTERFACE
              value: eth0
        probe:
          - name: check-app-responsive
            type: httpProbe
            mode: Continuous
            httpProbe/inputs:
              url: http://{{app_name}}.{{app_name}}.svc:8080/healthz
              method:
                get:
                  criteria: "=="
                  responseCode: "200"
            runProperties:
              probeTimeout: 5s
              retry: 3
              interval: 10s
```

## Verification
- `kubectl get chaosresult {{app_name}}-network-latency-pod-network-latency -n {{app_name}}` — verdict is Pass
- The health endpoint returns 200 throughout the 60s latency injection window
- No pod restarts or crash loops are triggered by the induced latency
