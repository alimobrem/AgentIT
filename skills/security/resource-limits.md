---
name: resource-limits
domain: security
version: 1
triggers:
  - resource
  - quota
  - limit
  - memory
  - cpu
outputs:
  - ResourceQuota
  - LimitRange
property: "Namespace has resource quotas and default container limits"
mode: template
---

# Resource Limits

## Property
Every namespace has a ResourceQuota preventing unbounded resource
consumption, and a LimitRange setting sensible defaults for containers
that don't specify their own limits.

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
---
apiVersion: v1
kind: LimitRange
metadata:
  name: {{app_name}}-limits
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  limits:
    - type: Container
      default:
        cpu: 500m
        memory: 512Mi
      defaultRequest:
        cpu: 100m
        memory: 256Mi
```
