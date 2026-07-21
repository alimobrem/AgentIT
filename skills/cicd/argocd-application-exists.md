---
name: argocd-application-exists
domain: cicd
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: gitops
description: No GitOps configuration (Argo CD) detected
recommendation: Create Argo CD Application for GitOps delivery
rule:
  type: file_contains
  pattern: argoproj.io
status: active
source: manual
---

# Argo CD Application Exists Check

## Property
Every application should be delivered via GitOps (an Argo CD
`Application` manifest committed to the repo), not ad hoc `kubectl apply`.

## Rule
Fires unless some file in the repo contains the string `argoproj.io`
(the API group every Argo CD `Application`/`AppProject` manifest uses).

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/cicd/gitops.yaml`, byte-for-byte the same
  rule (single `file_contains: argoproj.io` pattern, same
  dimension/severity/category/description/recommendation) -- Phase 4 of
  docs/extension-model-unification-plan-2026-07-18.md. Proven equivalent
  by `tests/test_phase4_check_migrations.py`'s
  `TestArgocdApplicationExistsParity` before the YAML file was deleted.
  Deliberately not tightened to `yaml_kind_exists: Application` (which
  would only match a real Argo CD `Application` object, not e.g. a
  README merely mentioning the API group) -- kept exactly as narrow/broad
  as the original check, per this phase's own "no silent behavior change"
  constraint.

## Verification
- Repo contains an `argocd/application.yaml` (or similar) with
  `apiVersion: argoproj.io/v1alpha1`.
