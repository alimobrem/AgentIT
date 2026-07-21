---
name: multi-replica-deployment
domain: ha_dr
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: availability
description: No multi-replica deployment found -- no redundancy
recommendation: Set replicas >= 2 for high availability
rule:
  type: file_contains
  pattern: "replicas: 2"
status: active
source: manual
---

# Multi-Replica Deployment Check

## Property
Every application should run at least 2 replicas so a single pod
restart/eviction never causes a full outage.

## Rule
Fires unless some file in the repo contains the literal string
`replicas: 2`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/ha_dr/replicas.yaml`, byte-for-byte the
  same rule (single `file_contains: "replicas: 2"` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s
  `TestMultiReplicaDeploymentParity` before the YAML file was deleted.
  ha_dr dimension fully migrated after this port (3/3 checks).
- Deliberately kept as the exact literal `"replicas: 2"` string match,
  not broadened to `replicas: [2-9]` or similar during this port -- the
  original check's own narrow literal-match behavior (a deployment with
  `replicas: 3` would *not* satisfy this exact check) is preserved
  byte-for-byte, per this phase's "no silent behavior change" constraint.

## Verification
- `kubectl get deployment <name> -o jsonpath='{.spec.replicas}'` shows
  `2` or more.
