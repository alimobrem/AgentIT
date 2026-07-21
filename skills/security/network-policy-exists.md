---
name: network-policy-exists
domain: security
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: network
description: No NetworkPolicy manifests found
recommendation: Add deny-all NetworkPolicy + allow rules
rule:
  type: yaml_kind_exists
  pattern: NetworkPolicy
status: active
source: manual
---

# Network Policy Exists Check

## Property
Every application's namespace should have explicit `NetworkPolicy`
manifests (deny-all baseline + allow rules) rather than relying on an
open-by-default network posture.

## Rule
Fires unless some YAML file in the repo contains `kind: NetworkPolicy`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/security/network-policy.yaml`,
  byte-for-byte the same rule (single `yaml_kind_exists: NetworkPolicy`
  pattern, same dimension/severity/category/description/recommendation)
  -- Phase 4 of docs/extension-model-unification-plan-2026-07-18.md.
  Proven equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestNetworkPolicyExistsParity` before the YAML file was deleted.
- Distinct from `skills/security/network-policy.md`, which *generates*
  the NetworkPolicy manifest (`mode: template`) -- this skill only
  *detects* whether one already exists in the repo. Named
  `network-policy-exists` (not `network-policy`) specifically to avoid
  colliding with that skill's own name.

## Verification
- `kubectl get networkpolicy -n <namespace>` shows the deny-all/allow
  NetworkPolicy manifests generated for this app.
