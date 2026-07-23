---
name: sbom-ci
domain: compliance
version: 1
triggers:
  - sbom
  - bom
  - software
  - bill
outputs:
  - .github/workflows/sbom.yml
delivery: source
property: "CI generates a CycloneDX Software Bill of Materials on every build"
mode: template
---

# SBOM CI Generation (source patch)

## Property
App CI generates a CycloneDX SBOM so Assess can clear compliance `sbom`.

## Product path
Clearance is **CI generation**, not a committed static `sbom.cdx.json`.

1. Prefer **GitHub Actions** when `.github/workflows/` exists — add
   `anchore/sbom-action` to an existing workflow, or add
   `.github/workflows/sbom.yml`
2. Else if the repo already has a Tekton **Pipeline**, wire an
   `sbom-generate` taskRef (app CI)
3. Else create `.github/workflows/sbom.yml` (easy default)

## Why not sbom-task / sbom-artifact
- Bare cluster Tekton `sbom-task` does **not** clear Assess unless wired
  into a Pipeline that runs for the app (wrong-layer).
- Static `sbom-artifact` (`sbom.cdx.json`) is demoted — optional fallback
  only when no CI can be added; not the primary auto_pr path.

## Constraints
- `delivery: source` only
- Clear-evidence `sbom_ci` requires staged workflow/Pipeline content with
  `anchore/sbom-action` or Syft generate / Pipeline sbom wire
- Refuses static CycloneDX file and bare Task YAML as clear evidence

## Verification
- Workflow contains `anchore/sbom-action` (or Syft cyclonedx/spdx step)
- Or Tekton Pipeline includes `sbom-generate` / `*-sbom` taskRef
- Re-Assess: `sbom` finding resolved
