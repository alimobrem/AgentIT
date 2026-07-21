---
name: containerfile-exists
domain: security
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: container
description: No Containerfile found for container builds
recommendation: Create a multi-stage Containerfile using UBI base image
rule:
  type: file_exists
  pattern: "Containerfile*"
status: active
source: manual
---

# Containerfile Exists Check

## Property
Every application should have a `Containerfile` (or
`Containerfile.<variant>`) committed to its own repo, using a UBI base
image for a security-hardened, Red-Hat-supported build.

## Rule
Fires unless the repo contains a file matching the glob `Containerfile*`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/security/containerfile.yaml`,
  byte-for-byte the same rule (single `file_exists: "Containerfile*"`
  pattern, same dimension/severity/category/description/recommendation)
  -- Phase 4 of docs/extension-model-unification-plan-2026-07-18.md.
  Proven equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestContainerfileExistsParity` before the YAML file was deleted.
- Distinct from `checks/cicd/dockerfile.yaml`'s own separate `Dockerfile*`
  check (now `skills/cicd/dockerfile-exists.md`): the two dimensions
  (security vs. cicd) intentionally check for different filenames, not
  consolidated into one rule during this port. Also distinct from
  `skills/security/containerfile.md`, which *generates* a Containerfile
  (`mode: template`) -- this skill only *detects* whether one already
  exists. Named `containerfile-exists` (not `containerfile`) specifically
  to avoid colliding with that skill's own name.

## Verification
- `ls Containerfile*` in the app's own repo.
