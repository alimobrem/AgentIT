---
name: sbom-artifact
domain: compliance
version: 1
triggers:
  - bom
  - cyclonedx
outputs:
  - sbom.cdx.json
delivery: source
property: "Application repo carries a CycloneDX Software Bill of Materials artifact"
mode: template
---

# SBOM Artifact (source patch) — demoted fallback

> **Not the primary auto_pr path.** Compliance `sbom` clears via
> **`sbom-ci`** (CI generation). This skill remains only as an optional
> fallback when no CI can be wired, with honest messaging that Assess
> still expects CI SBOM generation.

## Property
App repo carries a CycloneDX SBOM file. Prefer CI generation instead.

## Why demoted
Product truth: Assess / clear-evidence look for CI SBOM steps
(`anchore/sbom-action`, Syft workflow, Tekton Pipeline wire) — not a
committed static `sbom.cdx.json`. Scan must not open this as the primary
remediation for `sbom` (`SOLUTION_CONTRACTS` refuses it as companion).

## Constraints
- Real CycloneDX at `sbom.cdx.json` with **non-empty** `components`
- Does **not** satisfy clear-evidence `sbom_ci`
- `delivery: source` only

## Verification
- Prefer: merge a `sbom-ci` workflow PR, then re-Assess
- This artifact alone will **not** clear the `sbom` finding on tip
