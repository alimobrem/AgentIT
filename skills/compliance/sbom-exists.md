---
name: sbom-exists
domain: compliance
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: sbom
description: No SBOM (Software Bill of Materials) found
recommendation: Generate SBOM using Syft, store in ODF
rule:
  type: file_exists
  pattern: "*sbom*"
status: active
source: manual
---

# SBOM Exists Check

## Property
Every application's repo should carry a Software Bill of Materials so
its dependency provenance is auditable.

## Rule
Fires unless the repo contains a file whose name matches the glob
`*sbom*` (case-sensitive, mirroring the deleted YAML's exact glob).

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/compliance/sbom.yaml`, byte-for-byte the
  same rule (single `file_exists: "*sbom*"` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s `TestSbomExistsParity`
  before the YAML file was deleted. Distinct from (and not merged with)
  `skills/compliance/sbom-task.md`'s own remediation trigger set -- that
  skill generates a Tekton `Task` for SBOM generation; this check only
  detects whether an SBOM artifact already exists in the repo.

## Verification
- `ls *sbom*` in the app's own repo (e.g. `sbom.json`, `sbom-report.spdx`).
