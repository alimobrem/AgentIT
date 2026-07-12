---
name: security-context
domain: security
version: 1
triggers:
  - security
  - root
  - privilege
  - container
outputs:
  - SecurityContext
property: "Containers run with least-privilege security settings"
mode: template
---

# Security Context

## Property
All containers run as non-root with read-only root filesystem,
dropped capabilities, and no privilege escalation.

## Template

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: {{app_name}}-security-patch
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: app
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: [ALL]
        readOnlyRootFilesystem: true
```

## Verification
- kubectl get pod -o jsonpath='{.spec.securityContext.runAsNonRoot}' → true
- kubectl get pod -o jsonpath='{.spec.containers[0].securityContext.allowPrivilegeEscalation}' → false
