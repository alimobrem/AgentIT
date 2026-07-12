---
name: kyverno-require-labels
domain: compliance
version: 1
triggers:
  - policy
  - compliance
  - label
  - governance
outputs:
  - Policy
property: "All resources have required labels for governance"
mode: template
---

# Kyverno Label Policy

## Property
Every pod must have app.kubernetes.io/name and app.kubernetes.io/managed-by
labels. This ensures all resources are identifiable and attributable.

## Template

```yaml
apiVersion: kyverno.io/v1
kind: Policy
metadata:
  name: {{app_name}}-require-labels
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  validationFailureAction: Audit
  rules:
    - name: require-labels
      match:
        any:
          - resources:
              kinds:
                - Pod
      validate:
        message: "Labels app.kubernetes.io/name and app.kubernetes.io/managed-by are required"
        pattern:
          metadata:
            labels:
              app.kubernetes.io/name: "?*"
              app.kubernetes.io/managed-by: "?*"
```
