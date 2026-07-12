---
name: audit-policy
domain: compliance
version: 1
triggers:
  - audit
  - logging
  - compliance
  - governance
outputs:
  - Policy
property: "Kubernetes audit policy logs all security-relevant operations"
mode: template
---

# Kubernetes Audit Policy

## Property
All security-relevant API server operations (create, update, patch,
delete on pods, services, secrets, deployments, and RBAC resources)
are logged at RequestResponse level. Secret reads are logged at
Metadata level to avoid leaking sensitive data.

## Constraints
- RequestResponse level for mutating verbs on security-sensitive resources
- Metadata level for get/list/watch on secrets (captures access without logging content)
- Policy must include metadata.name for identification
- Applied to the API server via --audit-policy-file

## Template

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
metadata:
  name: {{app_name}}-audit-policy
rules:
  # Log secret reads at Metadata level (avoid leaking secret data)
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets"]
    verbs: ["get", "list", "watch"]

  # Log all mutating operations on security-sensitive resources
  - level: RequestResponse
    resources:
      - group: ""
        resources: ["pods", "services", "secrets"]
      - group: "apps"
        resources: ["deployments"]
      - group: "rbac.authorization.k8s.io"
        resources: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
    verbs: ["create", "update", "patch", "delete"]

  # Default: log metadata for everything else
  - level: Metadata
    omitStages:
      - RequestReceived
```

## Verification
- Audit log file contains entries for pod create/delete operations
- Secret read events appear with Metadata level (no request/response body)
- RBAC changes are logged with full RequestResponse detail
