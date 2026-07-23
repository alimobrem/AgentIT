---
name: argocd-application
domain: cicd
version: 1
triggers:
  - gitops
  - argocd
  - argo
  - deployment
  - sync
outputs:
  - Application
property: "Application is deployed via GitOps with auto-sync"
mode: template
---

# Argo CD Application — GitOps Deployment

## Property
The application is deployed and managed through Argo CD GitOps,
with automated sync, self-healing, and pruning enabled so the
cluster state always matches the Git source of truth.

## Constraints
- Uses argoproj.io/v1alpha1 Application CRD
- Automated sync with self-heal and prune enabled
- Retry policy for transient failures (5 attempts, backoff)
- Destination is the in-cluster default server
- `spec.source` **must** include non-empty `repoURL` and `path` **or** `chart`
- Do not invent `path: deploy/` when that tree is missing — prefer `chart/`
  when present; clear-evidence `argocd_application` refuses empty Application
  / bogus path when the repo tree is known
- Namespace is created automatically (CreateNamespace=true)

## Template

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {{app_name}}
  namespace: openshift-gitops
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/managed-by: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: "{{git_url}}"
    targetRevision: HEAD
    path: chart/
  destination:
    server: https://kubernetes.default.svc
    namespace: "{{namespace}}"
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

## Verification
- oc get application APP -n openshift-gitops — status is Synced and Healthy
- Modify a resource manually — Argo CD self-heals within sync interval
- Delete a resource from Git — Argo CD prunes it from the cluster
- Push a change to Git — Argo CD auto-syncs the new state
- Clear-evidence refuses Application without repoURL + path/chart
