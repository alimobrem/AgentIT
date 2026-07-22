---
name: hpa
domain: infrastructure
version: 2
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
- Minimum 2 replicas for high/critical criticality, 1 for low/medium — **except** when the workload mounts a ReadWriteOnce PVC (then maxReplicas must be 1, or skip HPA)
- Maximum 10 replicas only when storage allows multi-attach (RWX / no RWO data volume); never maxReplicas>1 against RWO
- Scale on CPU utilization at 80% threshold
- Use autoscaling/v2 API (not v2beta1 — deprecated since K8s 1.23)
- **scaleTargetRef MUST resolve to a live Deployment or Rollout** in the app namespace — never invent `Deployment/{{app_name}}` when the real workloads are `{{app_name}}-api` / `{{app_name}}-web` / `Rollout/{{app_name}}`
- Prefer exact `Rollout/{{app_name}}` when present (`apiVersion: argoproj.io/v1alpha1`); else exact `Deployment/{{app_name}}`; else existing `{{app_name}}-api|web|worker|…` Deployments
- When the chart uses Argo Rollouts (`kind: Rollout` / `rollout.enabled`): target `apiVersion: argoproj.io/v1alpha1`, `kind: Rollout` — not `apps/v1` Deployment
- Prefer emitting nothing (empty) over an HPA that would not attach or would Multi-Attach — fail closed into human review
- Fleet generation discovers Deployments/Rollouts via the apiserver (`portal/fleet_hpa.py`) and refuses PRs whose `scaleTargetRef` is missing on the cluster

## Template (fleet / plain Deployment apps)

Only use this shape when a Deployment named `{{app_name}}` **actually exists**.
For multi-service apps, emit one HPA per live scalable Deployment (e.g. `{{app_name}}-api`), or target `Rollout/{{app_name}}` when that Rollout exists.

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{app_name}}-hpa
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

## Fleet / Argo Rollout (when Rollout/{{app_name}} exists)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{app_name}}-hpa
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  scaleTargetRef:
    apiVersion: argoproj.io/v1alpha1
    kind: Rollout
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

## Self-managed / Helm (AgentIT chart shape)

When generating for a Helm chart that already defines an Argo Rollout named
`{{ .Release.Name }}` and a ReadWriteOnce data PVC, use this shape (or output
nothing if you cannot satisfy RWO safety):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: "{{ .Release.Name }}"
  namespace: "{{ .Release.Namespace }}"
  labels:
    app.kubernetes.io/name: agentit
    app.kubernetes.io/instance: "{{ .Release.Name }}"
spec:
  scaleTargetRef:
    apiVersion: argoproj.io/v1alpha1
    kind: Rollout
    name: "{{ .Release.Name }}"
  minReplicas: 1
  maxReplicas: 1
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 80
```

## Verification
- kubectl get hpa — should show the HPA with TARGETS populated (not `<unknown>`)
- scaleTargetRef must resolve to a live Deployment or Rollout of the same name
- Under load (when maxReplicas > 1): replica count should increase above minReplicas
- After load: replica count should decrease back to minReplicas
- RWO-capped HPA (maxReplicas: 1): TARGETS populate; do not expect multi-replica scale-out
