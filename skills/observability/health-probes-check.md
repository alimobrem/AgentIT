---
name: health-probes-check
domain: observability
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: health
description: No liveness/readiness probes detected in manifests
recommendation: Add livenessProbe and readinessProbe to all containers
rule:
  type: file_contains
  pattern: livenessProbe
status: active
source: manual
---

# Health Probes Check

## Property
Every container should define a `livenessProbe` so Kubernetes can detect
a hung/unready process and restart the pod.

## Rule
Fires unless at least one manifest in the repo contains `livenessProbe`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-18 from `checks/observability/health-check.yaml`,
  byte-for-byte the same rule (single `file_contains: livenessProbe`
  pattern, same dimension/severity/category/description/recommendation)
  as the Phase 1 proof-of-concept for the agents+skills+checks
  unification (see `docs/extension-model-unification-plan-2026-07-18.md`)
  -- proven equivalent by `tests/test_skill_engine.py`'s
  `TestDetectModeParity` before the YAML file was deleted. Now a single
  git-trackable Markdown file with a full lifecycle (draft/active/
  deprecated/retired) and the same Activate/Deprecate UI every
  remediation skill already has, instead of a YAML file with no
  lifecycle of its own. A separate rule (Gap 1's list-pattern OR, e.g.
  `pattern: [livenessProbe, readinessProbe]`) is available to any
  detect-mode skill that wants to match multiple keywords -- deliberately
  not used here, to keep this specific port an exact, provable parity
  migration rather than a silent behavior change bundled into it.

## Verification
- `kubectl get deployment <name> -o yaml` shows `livenessProbe`/
  `readinessProbe` under at least one container spec.
