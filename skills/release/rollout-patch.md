---
name: rollout-patch
domain: release
version: 1
triggers:
  - rollout
  - canary
  - progressive
  - deployment
outputs:
  - Rollout
property: "Deployments use analysis-gated canary steps"
mode: template
---

# Argo Rollout — Analysis-Gated Canary

## Property
Deployments use progressive canary rollout with automated analysis
gates at each traffic step. Each stage runs the AnalysisTemplate
before advancing, automatically rolling back on metric failure.

## Constraints
- Uses argoproj.io/v1alpha1 Rollout CRD
- Canary strategy with analysis at each weight step
- References the app's AnalysisTemplate for metric verification
- Steps: 10% (analyze) → 30% (analyze) → 60% (analyze) → 100%
- Anti-affinity ensures canary and stable pods spread across nodes
- Automatic rollback on analysis failure or progress deadline

## Template

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: rollout
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
          image: {{image}}
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app.kubernetes.io/name: {{app_name}}
                topologyKey: kubernetes.io/hostname
  strategy:
    canary:
      canaryService: {{app_name}}-canary
      stableService: {{app_name}}-stable
      analysis:
        templates:
          - templateName: {{app_name}}-canary-analysis
        args:
          - name: service-name
            value: {{app_name}}
      steps:
        - setWeight: 10
        - analysis:
            templates:
              - templateName: {{app_name}}-canary-analysis
            args:
              - name: service-name
                value: {{app_name}}
        - setWeight: 30
        - analysis:
            templates:
              - templateName: {{app_name}}-canary-analysis
            args:
              - name: service-name
                value: {{app_name}}
        - setWeight: 60
        - analysis:
            templates:
              - templateName: {{app_name}}-canary-analysis
            args:
              - name: service-name
                value: {{app_name}}
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
- kubectl argo rollouts get rollout {{app_name}} — shows analysis-gated steps
- AnalysisRun created at each weight step before promotion
- Rollout aborts and rolls back if analysis fails at any step
- Both stable and canary Services exist and route correctly
