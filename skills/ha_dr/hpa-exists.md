---
name: hpa-exists
domain: ha_dr
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: scaling
description: No HorizontalPodAutoscaler defined
recommendation: Add HPA for automatic scaling under load
rule:
  type: yaml_kind_exists
  pattern: HorizontalPodAutoscaler
status: active
source: manual
---

# HPA Exists Check (HA/DR)

## Property
Every application should scale automatically under load rather than
running a fixed replica count that can't absorb a traffic spike.

## Rule
Fires unless some YAML file in the repo contains
`kind: HorizontalPodAutoscaler`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/ha_dr/hpa.yaml`, byte-for-byte the same
  rule (single `yaml_kind_exists: HorizontalPodAutoscaler` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s `TestHpaExistsParity`
  before the YAML file was deleted.
- Distinct from `skills/infrastructure/hpa.md`, which *generates* the HPA
  manifest (`domain: infrastructure`, `mode: template`) -- this skill only
  *detects* whether one already exists in the repo, under the `ha_dr`
  dimension the original check used, not `infrastructure`. Named
  `hpa-exists` (not `hpa`) specifically to avoid colliding with that
  skill's own name.

## Verification
- `kubectl get hpa` shows the HPA generated for this app.
