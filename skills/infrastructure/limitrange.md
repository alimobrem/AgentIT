---
name: limitrange
domain: infrastructure
version: 1
triggers:
  - limit
  - default
  - container
  - resources
outputs:
  - LimitRange
property: "Containers have default resource limits"
mode: template
---

# Limit Range

## Property
Every namespace has a LimitRange that sets default CPU and memory requests
and limits for containers that don't specify their own, preventing
unbound resource consumption and noisy-neighbor problems.

## Template

```yaml
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
      max:
        cpu: "2"
        memory: 2Gi
      min:
        cpu: 50m
        memory: 64Mi
```

## Verification
- `kubectl describe limitrange {{app_name}}-limits -n NS` shows defaults and bounds
- Deploy a pod without resource specs → inspect pod YAML to confirm defaults injected
