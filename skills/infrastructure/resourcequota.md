---
name: resourcequota
domain: infrastructure
version: 1
triggers:
  - quota
  - resource
  - limit
  - governance
  - namespace
outputs:
  - ResourceQuota
property: "Namespace has resource quotas preventing unbounded consumption"
mode: template
---

# Resource Quota

## Property
Every namespace has a ResourceQuota that caps CPU, memory, and pod count,
preventing any single team or workload from monopolizing cluster resources.

## Template

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: {{app_name}}-quota
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 8Gi
    limits.cpu: "8"
    limits.memory: 16Gi
    pods: "20"
    persistentvolumeclaims: "10"
    services: "10"
```

## Verification
- `kubectl describe quota {{app_name}}-quota -n NS` shows used vs hard limits
- Attempt to create a pod exceeding the quota → rejected by admission controller
