---
name: prometheus-metrics-exists
domain: observability
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: metrics
description: No Prometheus ServiceMonitor found
recommendation: Create ServiceMonitor for Prometheus scraping
rule:
  type: yaml_kind_exists
  pattern: ServiceMonitor
status: active
source: manual
---

# Prometheus Metrics Exists Check

## Property
Every application should expose metrics to Prometheus via a
`ServiceMonitor`, so its health/performance is observable without a
human manually scraping an endpoint.

## Rule
Fires unless some YAML file in the repo contains `kind: ServiceMonitor`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/observability/metrics-endpoint.yaml`,
  byte-for-byte the same rule (single `yaml_kind_exists: ServiceMonitor`
  pattern, same dimension/severity/category/description/recommendation)
  -- Phase 4 of docs/extension-model-unification-plan-2026-07-18.md.
  Proven equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestPrometheusMetricsExistsParity` before the YAML file was deleted.
- Distinct from `skills/observability/service-monitor.md`, which
  *generates* the ServiceMonitor manifest (`mode: template`) -- this
  skill only *detects* whether one already exists in the repo. Named
  `prometheus-metrics-exists` (not `service-monitor`) specifically to
  avoid colliding with that skill's own name.

## Verification
- `kubectl get servicemonitor` shows the ServiceMonitor generated for
  this app.
