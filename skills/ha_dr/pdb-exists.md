---
name: pdb-exists
domain: ha_dr
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: availability
description: No PodDisruptionBudget defined
recommendation: Add PDB to prevent all pods being evicted during maintenance
rule:
  type: yaml_kind_exists
  pattern: PodDisruptionBudget
status: active
source: manual
---

# PDB Exists Check (HA/DR)

## Property
Every application should survive voluntary disruptions (node drain,
cluster upgrade) without all its pods being evicted simultaneously.

## Rule
Fires unless some YAML file in the repo contains
`kind: PodDisruptionBudget`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/ha_dr/pdb.yaml`, byte-for-byte the same
  rule (single `yaml_kind_exists: PodDisruptionBudget` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s `TestPdbExistsParity`
  before the YAML file was deleted.
- Distinct from `skills/infrastructure/pdb.md`, which *generates* the PDB
  manifest (`domain: infrastructure`, `mode: template`) -- this skill only
  *detects* whether one already exists in the repo, under the `ha_dr`
  dimension the original check used, not `infrastructure`. Named
  `pdb-exists` (not `pdb`) specifically to avoid colliding with that
  skill's own name.

## Verification
- `kubectl get pdb` shows the PDB generated for this app.
