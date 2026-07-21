---
name: secrets-scanning-in-ci
domain: security
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: scanning
description: No container or dependency vulnerability scanning detected in CI
recommendation: Add Trivy or ACS (StackRox) scanning to the CI pipeline
rule:
  type: file_contains
  pattern: trivy
status: active
source: manual
---

# Secrets/Vulnerability Scanning In CI Check

## Property
Every application's CI pipeline should run container/dependency
vulnerability scanning (Trivy or ACS/StackRox) before an image ships,
rather than shipping unscanned images.

## Rule
Fires unless some file in the repo contains the substring `trivy`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/security/secrets-scanning.yaml`,
  byte-for-byte the same rule (single `file_contains: trivy` pattern,
  same dimension/severity/category/description/recommendation) -- Phase 4
  of docs/extension-model-unification-plan-2026-07-18.md. Proven
  equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestSecretsScanningInCiParity` before the YAML file was deleted. This
  is the **last** `checks/*.yaml` file in the repo -- `checks/` is now
  fully empty, and every dimension's detection rules are `mode: detect`
  skills.
- Deliberately kept as a `trivy`-only literal match, not broadened to
  also recognize ACS/StackRox scanning (which the recommendation text
  offers as an alternative) -- the original check's own narrow scope is
  preserved byte-for-byte during this port, matching this phase's own
  "no silent behavior change" constraint. The check's own name
  (`secrets-scanning-in-ci`) is a slight misnomer inherited unchanged
  from the original YAML -- it actually detects *vulnerability* scanning
  (its own description says so), not secrets scanning; renaming it was
  out of scope for a byte-for-byte port and would itself be a behavior
  change to every existing reference to this check's name.

## Verification
- CI pipeline definition (`.gitlab-ci.yml`/Tekton `Pipeline`) references
  `trivy` as a scan step.
