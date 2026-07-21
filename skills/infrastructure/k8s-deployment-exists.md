---
name: k8s-deployment-exists
domain: infrastructure
version: 1
mode: detect
triggers: []
outputs: []
severity: high
category: manifests
description: No Kubernetes Deployment manifests found
recommendation: Create deployment, service, and ingress manifests
rule:
  type: yaml_kind_exists
  pattern: Deployment
status: active
source: manual
---

# Kubernetes Deployment Exists Check

## Property
Every application should have a real Kubernetes `Deployment` manifest
committed to its repo -- the minimal artifact required to run it on a
cluster at all.

## Rule
Fires unless some YAML file in the repo contains `kind: Deployment`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/infrastructure/k8s-manifests.yaml`,
  byte-for-byte the same rule (single `yaml_kind_exists: Deployment`
  pattern, same dimension/severity/category/description/recommendation)
  -- Phase 4 of docs/extension-model-unification-plan-2026-07-18.md.
  Proven equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestK8sDeploymentExistsParity` before the YAML file was deleted.

## Verification
- `kubectl get deployment` shows the app's Deployment.
