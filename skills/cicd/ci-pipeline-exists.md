---
name: ci-pipeline-exists
domain: cicd
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: pipeline
description: No GitLab CI pipeline configuration found
recommendation: Create .gitlab-ci.yml or Tekton Pipeline for build/test/scan/deploy
rule:
  type: file_exists
  pattern: ".gitlab-ci.yml"
status: active
source: manual
---

# CI Pipeline Exists Check

## Property
Every application should have a CI pipeline definition committed to its
own repo so builds/tests/scans/deploys are reproducible and not
tribal-knowledge shell commands.

## Rule
Fires unless the repo contains a `.gitlab-ci.yml` file.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/cicd/ci-pipeline.yaml`, byte-for-byte the
  same rule (single `file_exists: ".gitlab-ci.yml"` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s
  `TestCiPipelineExistsParity` before the YAML file was deleted.
- Deliberately narrower than `skills/cicd/tekton-pipeline.md`'s own
  remediation trigger set: this check only recognizes a GitLab CI file,
  not a Tekton `Pipeline` manifest, mirroring the original YAML check's
  own scope exactly (not silently broadened during the port).

## Verification
- `ls .gitlab-ci.yml` in the app's own repo.
