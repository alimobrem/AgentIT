---
name: license-file-exists
domain: compliance
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: license
description: No LICENSE file found
recommendation: Add a LICENSE file (Apache 2.0 recommended for enterprise open source)
rule:
  type: file_exists
  pattern: "LICENSE*"
status: active
source: manual
---

# License File Exists Check

## Property
Every application's repo should carry an explicit LICENSE file so its
usage/redistribution terms are unambiguous.

## Rule
Fires unless the repo contains a file matching the glob `LICENSE*`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/compliance/license.yaml`, byte-for-byte
  the same rule (single `file_exists: "LICENSE*"` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s
  `TestLicenseFileExistsParity` before the YAML file was deleted.

## Verification
- `ls LICENSE*` in the app's own repo.
