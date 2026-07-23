---
name: image-signing-exists
domain: security
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: image_signing
description: No cosign/Sigstore image signing detected in CI or Tekton
recommendation: Add a cosign sign (keyless Sigstore) Tekton Task for built images — use cosign-sign-task
rule:
  type: file_contains
  pattern: cosign
  case_insensitive: true
status: active
source: manual
---

# Image Signing Exists Check

## Property
Every application's CI / Tekton pipeline should cosign-sign (or attest)
built container images so consumers can verify provenance — without
claiming SLSA L3 or dumping hermetic/Konflux catalog theater.

## Rule
Fires unless some file in the repo contains the substring `cosign`
(case-insensitive). Typical clears: a Tekton `Task` that runs
`cosign sign` / `cosign attest`, or CI that invokes the cosign CLI.

## Constraints
- This is a detection-only skill (`mode: detect`) — it produces a
  `Finding` when the rule fails. See `skill_engine.detect_check_definitions()`.
- Category `image_signing` is remediable via `cosign-sign-task`
  (`SOLUTION_CONTRACTS` / clear-evidence `cosign_sign_task`).
- Deliberately narrow: literal `cosign` match (same discipline as
  `secrets-scanning-in-ci`'s `trivy` pattern). Does **not** treat
  "SLSA", "hermetic", or "Konflux" prose as a pass.

## Verification
- CI / Tekton definition references `cosign` (sign or attest step).
- Re-Assess: `image_signing` finding resolved after merge.
