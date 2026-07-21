---
name: dockerfile-exists
domain: cicd
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: container
description: No Dockerfile found for container builds
recommendation: Create multi-stage Dockerfile with UBI base image
rule:
  type: file_exists
  pattern: "Dockerfile*"
status: active
source: manual
---

# Dockerfile Exists Check

## Property
Every application should have a `Dockerfile` (or `Dockerfile.<variant>`)
committed to its own repo so its container image is built reproducibly.

## Rule
Fires unless the repo contains a file matching the glob `Dockerfile*`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/cicd/dockerfile.yaml`, byte-for-byte the
  same rule (single `file_exists: "Dockerfile*"` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s `TestDockerfileExistsParity`
  before the YAML file was deleted.
- Distinct from `checks/security/containerfile.yaml`'s own separate
  `Containerfile*` check (now `skills/security/containerfile-exists.md`):
  the two dimensions (cicd vs. security) intentionally check for
  different filenames, not consolidated into one rule during this port.

## Verification
- `ls Dockerfile*` in the app's own repo.
