---
name: sbom-exists
domain: compliance
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: sbom
description: No SBOM generation in CI
recommendation: Add CI SBOM generation (GitHub Action anchore/sbom-action or Syft step, or wire a Tekton Pipeline sbom task)
rule:
  type: file_contains
  pattern:
    - anchore/sbom-action
    - sbom-generate
  case_insensitive: true
status: active
source: manual
---

# SBOM Exists Check (CI generation)

## Property
Every application's CI should generate a Software Bill of Materials so
dependency provenance is auditable on every build — not a one-shot
committed static file.

## Rule
Fires unless some file contains `anchore/sbom-action` or `sbom-generate`
(case-insensitive). Typical clears: GitHub Action SBOM step, or a Tekton
Pipeline task named `sbom-generate`.

The ComplianceAnalyzer is the authoritative detector (also accepts Syft
in workflow/GitLab CI and Pipeline wiring). This detect skill is a
narrow `file_contains` complement so catalogs stay aligned.

## Constraints
- This is a detection-only skill (`mode: detect`).
- A committed `*sbom*` / `sbom.cdx.json` file alone does **not** clear.
- Bare cluster `sbom-task` without Pipeline wiring does **not** clear
  (wrong-layer). Remediation: `sbom-ci` (`SOLUTION_CONTRACTS` /
  clear-evidence `sbom_ci`).

## Verification
- `.github/workflows/*` uses `anchore/sbom-action`, or Pipeline has
  `sbom-generate`
- Re-Assess: `sbom` finding resolved
