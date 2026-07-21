---
name: helm-chart-exists
domain: infrastructure
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: iac
description: No Helm chart detected
recommendation: Generate Helm chart with values.yaml and environment overlays
rule:
  type: file_exists
  pattern: Chart.yaml
status: active
source: manual
---

# Helm Chart Exists Check

## Property
Every application should be packaged as a Helm chart so its manifests
are parameterized, versioned, and deployable across environments without
hand-editing YAML.

## Rule
Fires unless the repo contains a file named `Chart.yaml`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/infrastructure/helm-chart.yaml`,
  byte-for-byte the same rule (single `file_exists: Chart.yaml` pattern,
  same dimension/severity/category/description/recommendation) -- Phase 4
  of docs/extension-model-unification-plan-2026-07-18.md. Proven
  equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestHelmChartExistsParity` before the YAML file was deleted.

## Verification
- `ls Chart.yaml` in the app's own repo.
