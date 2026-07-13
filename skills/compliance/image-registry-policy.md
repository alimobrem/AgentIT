---
name: image-registry-policy
domain: compliance
version: 1
triggers:
  - registry
  - image
  - container
  - policy
  - compliance
outputs:
  - Policy
property: "All container images come from trusted, vetted registries"
mode: llm
---

# Image Registry Policy

## Property
All container images must come from Red Hat trusted registries.
Docker Hub, GitHub Container Registry, and other unvetted public
registries are not allowed — their images are not scanned, signed,
or supported by Red Hat.

## Trusted registries
- `registry.access.redhat.com` — Red Hat Container Catalog (UBI, RHEL, middleware)
- `registry.redhat.io` — Red Hat authenticated registry
- `quay.io` — Red Hat Quay (supports image signing and scanning)
- `image-registry.openshift-image-registry.svc` — OpenShift internal registry

## Why not Docker Hub
- No SLA on image availability
- No guaranteed vulnerability scanning
- No image signing by default
- Supply chain attacks via typosquatting
- Images may contain unknown licenses
- Cannot be audited for compliance

## What to generate
A Kyverno Policy that validates all container images come from
the trusted registries listed above. Use `validationFailureAction: Audit`
initially (warn, don't block) so teams can migrate gradually.

## Constraints
- Use `kyverno.io/v1` API
- Use namespace-scoped `Policy`, not `ClusterPolicy`
- Include all 4 trusted registries in the allow list
- Include the OpenShift internal registry (apps built on-cluster use it)
- Set `validationFailureAction: Audit` (not Enforce) by default

## Template

```yaml
apiVersion: kyverno.io/v1
kind: Policy
metadata:
  name: {{app_name}}-trusted-registries
  labels:
    app.kubernetes.io/name: {{app_name}}
spec:
  validationFailureAction: Audit
  rules:
    - name: validate-registries
      match:
        any:
          - resources:
              kinds:
                - Pod
              selector:
                matchLabels:
                  app.kubernetes.io/name: {{app_name}}
      validate:
        message: "Images must come from a trusted registry: registry.access.redhat.com, registry.redhat.io, quay.io, or the OpenShift internal registry."
        pattern:
          spec:
            containers:
              - image: "registry.access.redhat.com/* | registry.redhat.io/* | quay.io/* | image-registry.openshift-image-registry.svc*/*"
```

## Verification
- Deploy a pod with `image: docker.io/nginx` → should trigger audit warning
- Deploy a pod with `image: registry.access.redhat.com/ubi9/ubi-minimal` → should pass
