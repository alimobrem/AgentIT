---
name: rbac
domain: security
version: 1
triggers:
  - rbac
  - serviceaccount
  - authorization
  - privilege
  - permission
outputs:
  - ServiceAccount
  - Role
  - RoleBinding
property: "Application runs with least-privilege access"
mode: template
---

# RBAC — Least Privilege Access

## Property
The application runs under a dedicated ServiceAccount with only
the permissions it needs — no default SA, no cluster-admin,
no unnecessary API access.

## Constraints
- Dedicated ServiceAccount per application
- Role with minimal permissions (get/list/watch on configmaps and secrets)
- RoleBinding scoped to the application's namespace
- Use rbac.authorization.k8s.io/v1 API

## Template

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{app_name}}
subjects:
  - kind: ServiceAccount
    name: {{app_name}}
```

## Verification
- kubectl auth can-i --as=system:serviceaccount:NS:APP get pods → no
- kubectl auth can-i --as=system:serviceaccount:NS:APP get configmaps → yes
