---
name: audit-policy
domain: compliance
version: 2
triggers:
  - audit
  - logging
  - compliance
  - governance
outputs:
  - ConfigMap
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
- Delivered as advisory documentation, not a cluster apply attempt -- see
  "Why this isn't a manifest" below

## Why this isn't a manifest
`audit.k8s.io/v1 Policy` is **not** a Kubernetes REST API resource on any
cluster -- there is no `kubectl apply`-able `Policy` object in that API
group. It's a static file schema consumed only by the API server's own
`--audit-policy-file` startup flag (kube-apiserver) or, on OpenShift, by
the `config.openshift.io/v1 APIServer` cluster singleton's coarser
`spec.audit.profile`. A prior version of this skill emitted
`apiVersion: audit.k8s.io/v1, kind: Policy` as if it were an applyable
manifest -- guaranteed to fail on every cluster (no such API is ever
registered) and, worse, likely to be misattributed by the missing-operator
heuristic to a missing Kyverno install (Kyverno's own CRD is also
named `Policy`, in a completely different API group) -- a wrong, misleading
fix suggestion.

Since enabling audit logging is a cluster-admin, apiserver-level
configuration change AgentIT's service account cannot apply directly on
any cluster, this skill instead delivers the real audit policy rules as
reference documentation in a ConfigMap -- the same advisory-only pattern
`incident/runbook.md`, `retirement/decommission-plan.md`, and
`compliance/compliance-evidence.md` already use so the skill engine
(which only ever writes a single `.yaml` file per skill) still produces
a real, always-applyable K8s object instead of a bare markdown file. A
human with cluster-admin access reviews the ConfigMap and wires the
policy in via the documented steps -- it is never silently treated as
"already enforced."

## Template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{app_name}}-audit-policy
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: audit-policy
data:
  audit-policy.yaml: |
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
  README.md: |
    # Enabling this audit policy — {{app_name}}

    This ConfigMap holds a real, ready-to-use Kubernetes audit policy
    (`audit-policy.yaml`) -- it is **not** applied automatically, because
    audit logging is configured on the API server itself, not via a
    resource this service account can `kubectl apply`.

    ## Vanilla Kubernetes (kube-apiserver)
    1. Copy `audit-policy.yaml`'s contents to a file the API server's
       host/pod can read, e.g. `/etc/kubernetes/audit-policy.yaml`.
    2. Add `--audit-policy-file=/etc/kubernetes/audit-policy.yaml` and
       `--audit-log-path=<log destination>` to the kube-apiserver startup
       flags, mounting the file into the apiserver's static pod/container.
    3. Restart the API server for the flags to take effect.

    ## OpenShift
    OpenShift does not accept a custom `audit-policy.yaml` file directly --
    cluster-admins instead set an audit profile (or, on supported
    versions, custom per-group rules) on the cluster-scoped
    `config.openshift.io/v1 APIServer` singleton (`metadata.name: cluster`),
    e.g. (run as a cluster-admin, not by AgentIT):

      oc patch apiserver cluster --type=merge -p '{"spec":{"audit":{"profile":"WriteRequestBodies"}}}'

    This requires cluster-admin RBAC AgentIT's service account does not
    have. `audit-policy.yaml` in this ConfigMap remains useful as the
    fine-grained rule reference this profile-based approach approximates.
```

## Verification
- `kubectl get configmap {{app_name}}-audit-policy -o jsonpath='{.data}'` — the reference policy and README are present
- After a cluster-admin wires the policy in (see README.md above): the audit log file contains entries for pod create/delete operations
- Secret read events appear with Metadata level (no request/response body)
- RBAC changes are logged with full RequestResponse detail
