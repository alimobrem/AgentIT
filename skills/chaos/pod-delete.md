---
name: pod-delete
domain: chaos
version: 1
triggers:
  - chaos
  - resilience
  - resiliency
  - disruption
  - availability
outputs:
  - ChaosEngine
property: "Application recovers automatically when a pod is deleted"
mode: template
---

# Pod Delete — Resiliency Experiment

## Property
When a pod is deleted, the application recovers automatically (a
replacement pod becomes Ready) without manual intervention, verifying
the redundancy claimed by replica counts and PodDisruptionBudgets.

## Constraints
- Uses the LitmusChaos generic experiment `pod-delete` — not the
  non-standard `pod-kill` name that appears elsewhere in this codebase
- Uses `PODS_AFFECTED_PERC` (the real Litmus env var) rather than the
  invented `KILL_COUNT`
- Uses `labelSelector` for probe targeting, not a Kubernetes
  `fieldSelector` (which Litmus's `k8sProbe` does not accept)
- Chaos duration and interval are short (60s / 10s) to limit blast radius
- Requires a dedicated ChaosServiceAccount scoped to the target namespace

## Template

```yaml
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: {{app_name}}-pod-delete
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
    - name: pod-delete
      spec:
        components:
          env:
            - name: TOTAL_CHAOS_DURATION
              value: "60"
            - name: CHAOS_INTERVAL
              value: "10"
            - name: PODS_AFFECTED_PERC
              value: "50"
            - name: FORCE
              value: "false"
        probe:
          - name: check-pod-recovery
            type: k8sProbe
            mode: EOT
            k8sProbe/inputs:
              command:
                group: ""
                version: v1
                resource: pods
                namespace: {{app_name}}
                labelSelector: app.kubernetes.io/name={{app_name}}
            runProperties:
              probeTimeout: 60s
              retry: 3
              interval: 5s
```

## Verification
- `kubectl get chaosengine {{app_name}}-pod-delete -n {{app_name}}` — engineState is active
- `kubectl get chaosresult {{app_name}}-pod-delete-pod-delete -n {{app_name}}` — verdict is Pass
- The deleted pod is replaced and Ready within CHAOS_INTERVAL plus the probe timeout
