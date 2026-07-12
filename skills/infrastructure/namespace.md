---
name: namespace
domain: infrastructure
version: 1
triggers:
  - namespace
  - project
  - environment
outputs:
  - Namespace
property: "Application has a dedicated namespace with standard labels"
mode: template
---

# Namespace

## Property
Each application runs in a dedicated namespace with standard
Kubernetes labels for identification, cost attribution, and team ownership.

## Template

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: {{app_name}}
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/managed-by: agentit
```
