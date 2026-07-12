---
name: hpa
domain: infrastructure
version: 1
triggers:
  - scaling
  - autoscal
  - resource
  - replica
  - availability
outputs:
  - HorizontalPodAutoscaler
property: "Application scales automatically based on load"
mode: template
---

# Horizontal Pod Autoscaler

## Property
The application automatically scales up when CPU usage exceeds 80%
and scales back down when load decreases, maintaining availability
without manual intervention.

## Constraints
- Minimum 2 replicas for high/critical criticality, 1 for low/medium
- Maximum 10 replicas (adjustable by human override)
- Scale on CPU utilization at 80% threshold
- Use autoscaling/v2 API (not v2beta1 — deprecated since K8s 1.23)

## Template

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{app_name}}
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 80
```

## Verification
- kubectl get hpa — should show the HPA with TARGETS populated
- Under load: replica count should increase above minReplicas
- After load: replica count should decrease back to minReplicas
