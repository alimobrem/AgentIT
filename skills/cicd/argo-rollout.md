---
name: argo-rollout
domain: cicd
version: 1
triggers:
  - rollout
  - canary
  - deployment
  - release
  - progressive
outputs:
  - Rollout
  - Service
property: "Deployments use canary rollout with automatic rollback"
mode: template
---

# Argo Rollout — Canary Deployment

## Property
Application deployments use progressive canary rollout that shifts
traffic gradually (5% → 25% → 50% → 100%) with automatic rollback
on failure, preventing bad releases from impacting all users.

## Constraints
- Uses argoproj.io/v1alpha1 Rollout CRD (replaces Deployment)
- Canary strategy with 4 steps: 5%, 25%, 50%, 100%
- Pauses between steps for observation (30s at 5%, 60s at 25%, 60s at 50%)
- Stable and canary Services for traffic splitting
- Automatic rollback on pod failure (progressDeadlineSeconds)
- Labels consistent with app.kubernetes.io conventions

## Template

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  replicas: 3
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      app.kubernetes.io/name: {{app_name}}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{app_name}}
    spec:
      containers:
        - name: {{app_name}}
          image: "{{image}}"
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
  strategy:
    canary:
      canaryService: {{app_name}}-canary
      stableService: {{app_name}}-stable
      steps:
        - setWeight: 5
        - pause:
            duration: 30s
        - setWeight: 25
        - pause:
            duration: 60s
        - setWeight: 50
        - pause:
            duration: 60s
        - setWeight: 100
  progressDeadlineSeconds: 600
---
apiVersion: v1
kind: Service
metadata:
  name: {{app_name}}-stable
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  selector:
    app.kubernetes.io/name: {{app_name}}
  ports:
    - port: 8080
      targetPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: {{app_name}}-canary
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  selector:
    app.kubernetes.io/name: {{app_name}}
  ports:
    - port: 8080
      targetPort: 8080
```

## Verification
- kubectl argo rollouts get rollout APP — shows canary steps and current weight
- kubectl argo rollouts promote APP — advances to next step
- kubectl argo rollouts abort APP — triggers rollback to stable
- Both stable and canary Services exist and route to correct pods
