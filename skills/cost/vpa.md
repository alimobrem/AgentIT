---
name: vpa
domain: cost
version: 1
triggers:
  - vpa
  - autoscal
  - rightsize
  - cost
  - resources
outputs:
  - VerticalPodAutoscaler
property: "Resource requests are automatically right-sized based on usage"
mode: template
---

# Vertical Pod Autoscaler

## Property
Workloads have a VPA that monitors actual resource usage and automatically
adjusts CPU and memory requests, eliminating over-provisioning waste
and under-provisioning risk.

## Template

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: {{app_name}}-vpa
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{app_name}}
  updatePolicy:
    updateMode: "Auto"
  resourcePolicy:
    containerPolicies:
      - containerName: "*"
        minAllowed:
          cpu: 50m
          memory: 64Mi
        maxAllowed:
          cpu: "2"
          memory: 2Gi
        controlledResources:
          - cpu
          - memory
```

## Notes
- Use `updateMode: "Off"` for critical/stateful apps — VPA will recommend but not restart pods
- VPA and HPA should not both target CPU on the same workload
- Requires the VPA controller to be installed on the cluster

## Verification
- `kubectl describe vpa {{app_name}}-vpa -n NS` shows recommendations vs current
- After stabilization, compare actual usage to requests — gap should shrink
