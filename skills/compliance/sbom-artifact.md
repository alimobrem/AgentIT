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

## Constraints
- Real CycloneDX at `sbom.cdx.json` (clear-evidence `sbom_file` refuses `{}` / Task YAML)
- `delivery: source` only

## Verification
- `jq -e '.bomFormat == "CycloneDX"' sbom.cdx.json`
- Re-Assess: `sbom` finding resolved
