---
name: pdb
domain: infrastructure
version: 1
triggers:
  - disruption
  - availability
  - ha
  - eviction
outputs:
  - PodDisruptionBudget
property: "Application survives node maintenance without downtime"
mode: template
---

# Pod Disruption Budget

## Property
During voluntary disruptions (node drain, cluster upgrade),
at least one pod must remain running at all times.

## Constraints
- minAvailable: 1 (ensures at least one pod serves traffic during disruptions)
- Use policy/v1 API (not v1beta1 — removed in K8s 1.25)
- `selector.matchLabels` must match a **live** workload/Service (HPA
  pattern). Clear-evidence `selector_target` refuses empty / zero-match
  selectors — do not invent `app: {{app_name}}` when pods use other labels

## Template

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: {{app_name}}
```

## Verification
- kubectl get pdb — should show ALLOWED DISRUPTIONS >= 0
- During node drain: at least one pod remains Running
- Clear-evidence refuses PDB with empty or non-matching selector
