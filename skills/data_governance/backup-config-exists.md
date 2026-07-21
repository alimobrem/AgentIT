---
name: backup-config-exists
domain: data_governance
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: backup
description: No backup configuration detected
recommendation: Configure Crunchy PostgreSQL backup schedule or add backup CronJob
rule:
  type: file_contains
  pattern: backup
status: active
source: manual
---

# Backup Config Exists Check

## Property
Every stateful application should have a discoverable backup
configuration committed to its own repo (a CronJob, a Crunchy PostgreSQL
backup schedule, etc.) rather than relying on undocumented, out-of-band
backup procedures.

## Rule
Fires unless some file in the repo contains the substring `backup`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/data_governance/backup-config.yaml`,
  byte-for-byte the same rule (single `file_contains: backup` pattern,
  same dimension/severity/category/description/recommendation) -- Phase 4
  of docs/extension-model-unification-plan-2026-07-18.md. Proven
  equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestBackupConfigExistsParity` before the YAML file was deleted.

## Verification
- Repo contains a file referencing a backup schedule/CronJob (e.g.
  `backup-cronjob.yaml`, a Crunchy `PostgresCluster.spec.backups`
  section).
