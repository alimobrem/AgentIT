---
name: resource-quota-exists
domain: infrastructure
version: 1
mode: detect
triggers: []
outputs: []
severity: low
category: quota
description: No ResourceQuota defined for namespace governance
recommendation: Add ResourceQuota and LimitRange for namespace governance
rule:
  type: yaml_kind_exists
  pattern: ResourceQuota
status: active
source: manual
---

# Resource Quota Exists Check

## Property
Every application's namespace should have a `ResourceQuota` so runaway
resource consumption in that namespace can't starve the rest of the
cluster.

## Rule
Fires unless some YAML file in the repo contains `kind: ResourceQuota`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/infrastructure/resource-quota.yaml`,
  byte-for-byte the same rule (single `yaml_kind_exists: ResourceQuota`
  pattern, same dimension/severity/category/description/recommendation)
  -- Phase 4 of docs/extension-model-unification-plan-2026-07-18.md.
  Proven equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestResourceQuotaExistsParity` before the YAML file was deleted.
  infrastructure dimension fully migrated after this port (3/3 checks).
- Distinct from `skills/infrastructure/resourcequota.md`, which
  *generates* the ResourceQuota manifest (`mode: template`) -- this skill
  only *detects* whether one already exists in the repo. Named
  `resource-quota-exists` (not `resourcequota`) specifically to avoid
  colliding with that skill's own name.

## Verification
- `kubectl get resourcequota -n <namespace>` shows the quota generated
  for this app.
