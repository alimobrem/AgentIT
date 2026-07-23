---
name: cosign-sign-task
domain: security
version: 1
triggers:
  - signing
  - cosign
  - sigstore
  - attest
outputs:
  - Task
property: "Built container images are cosign-signed (keyless Sigstore) before promotion"
mode: template
delivery: cluster
---

# Cosign Sign Task — Image Signing

## Property
Built container images are signed with cosign (Sigstore keyless) so
downstream policy can verify signatures. This clears the
`image_signing` finding when the Task lands under fleet
`apps/{app}/` or self-managed `chart/templates/tekton/`.

## Constraints
- Uses the official cosign image — **pinned tag**, not `:latest`
- **Keyless Sigstore** by default (`cosign sign --yes`); no private keys
  committed to the repo
- Cluster must supply OIDC / workload identity for Fulcio (e.g. Tekton
  service account annotations or an identity provider your platform
  already trusts). Document required env — do not invent fake keys
- Optional: set `COSIGN_EXPERIMENTAL=1` only if your cosign build still
  requires it for keyless; prefer current `--yes` keyless flow
- Does **not** claim SLSA L3, hermetic builds, or Konflux "enterprise"
  theater — sign only
- Clear-evidence `cosign_sign_task` refuses empty Task stubs and
  SLSA/hermetic prose without a real `cosign sign` / `cosign attest`

## Secrets / env (operator notes — not committed)
| Env / secret | Purpose |
| --- | --- |
| Workload identity / SA OIDC | Fulcio identity for keyless sign |
| `COSIGN_YES=true` | Non-interactive confirm (also covered by `--yes`) |
| Registry pull/push credentials | Only if the image registry requires auth (use existing cluster pull secrets; do not bake tokens into YAML) |

## Template

```yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: {{app_name}}-cosign-sign
  labels:
    app.kubernetes.io/name: {{app_name}}
    app.kubernetes.io/component: image-signing
spec:
  description: >
    Cosign-sign a built image with Sigstore keyless (Fulcio + Rekor).
    Requires workload identity / OIDC for the TaskRun service account.
    Does not embed signing keys.
  params:
    - name: IMAGE
      type: string
      description: Full image reference to sign (digest preferred)
  steps:
    - name: cosign-sign
      image: gcr.io/projectsigstore/cosign:v2.4.3
      env:
        - name: COSIGN_YES
          value: "true"
      script: |
        #!/usr/bin/env sh
        set -eu
        # Keyless Sigstore: identity comes from the TaskRun SA OIDC token.
        # Prefer IMAGE digests (repo@sha256:…) over mutable tags.
        cosign sign --yes "$(params.IMAGE)"
        echo "Signed $(params.IMAGE)"
```

## Verification
- `tkn task describe {{app_name}}-cosign-sign` — Task exists with IMAGE param
- Task YAML contains `cosign sign` (clear-evidence pass)
- Re-Assess after merge: `image_signing` finding resolved
- Live sign requires registry auth + OIDC (platform-specific — out of band)
