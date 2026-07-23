---
name: sbom-artifact
domain: compliance
version: 1
triggers:
  - sbom
  - bom
  - software
  - bill
outputs:
  - sbom.cdx.json
delivery: source
property: "Application repo carries a CycloneDX Software Bill of Materials artifact"
mode: template
---

# SBOM Artifact (source patch)

## Property
App repo carries a CycloneDX SBOM so Assess can clear compliance `sbom`.

## Why not sbom-task
Cluster Tekton `sbom-task` does **not** clear Assess (file scan in app repo).
Same wrong-layer class as audit-policy vs app-audit-logging.

## How the BOM is built
1. Prefer **Syft** (`syft <repo> -o cyclonedx-json`) when available on PATH
2. Else inventory from lockfiles / manifests already in the repo:
   `requirements.txt`, `pyproject.toml`, `package.json` / `package-lock.json`,
   `go.mod`, `Pipfile`, `Cargo.toml`, `Gemfile`, `pom.xml`, `composer.json`
3. Delivery enrichment runs before clear-evidence so Scan/Deliver never open
   an empty shell

## Constraints
- Real CycloneDX at `sbom.cdx.json` with **non-empty** `components`
- Clear-evidence `sbom_file` refuses `{}`, Tekton Task YAML, and
  `components: []` theater
- `delivery: source` only

## Verification
- `jq -e '.bomFormat == "CycloneDX" and (.components | length) > 0' sbom.cdx.json`
- Re-Assess: `sbom` finding resolved
