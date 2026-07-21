---
name: retention-policy-exists
domain: data_governance
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: retention
description: No data retention policy detected
recommendation: Define data retention policies for compliance (GDPR, SOC 2)
rule:
  type: file_contains
  pattern: retention
status: active
source: manual
---

# Retention Policy Exists Check

## Property
Every application handling persistent data should document a data
retention policy in its own repo, so regulatory obligations (GDPR, SOC 2)
are traceable to a real, versioned artifact.

## Rule
Fires unless some file in the repo contains the substring `retention`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/data_governance/retention-policy.yaml`,
  byte-for-byte the same rule (single `file_contains: retention` pattern,
  same dimension/severity/category/description/recommendation) -- Phase 4
  of docs/extension-model-unification-plan-2026-07-18.md. Proven
  equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestRetentionPolicyExistsParity` before the YAML file was deleted.
  data_governance dimension fully migrated after this port (2/2 checks).

## Verification
- Repo contains a file documenting a retention policy (e.g.
  `docs/data-retention.md`, a `retentionPolicy` field in a Crunchy
  `PostgresCluster` backup spec).
