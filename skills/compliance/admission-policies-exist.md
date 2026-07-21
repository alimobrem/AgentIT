---
name: admission-policies-exist
domain: compliance
version: 1
mode: detect
triggers: []
outputs: []
severity: medium
category: policy
description: No admission policies (Kyverno/OPA/Gatekeeper) found
recommendation: Create Kyverno policies for resource limits, labels, approved base images
rule:
  type: yaml_kind_exists
  pattern: Policy
status: active
source: manual
---

# Admission Policies Exist Check

## Property
Every application's repo should carry its own admission-policy manifests
(Kyverno/OPA/Gatekeeper) rather than relying entirely on cluster-wide
policy nobody can see from the app's own source.

## Rule
Fires unless some YAML file in the repo contains `kind: Policy`.

## Constraints
- This is a detection-only skill (`mode: detect`) -- it produces a
  `Finding` when the rule fails, exactly like a legacy `checks/*.yaml`
  file, not a `GeneratedFile`. See `skill_engine.detect_check_definitions()`.
- Ported 2026-07-20 from `checks/compliance/admission-policies.yaml`,
  byte-for-byte the same rule (single `yaml_kind_exists: Policy` pattern,
  same dimension/severity/category/description/recommendation) -- Phase 4
  of docs/extension-model-unification-plan-2026-07-18.md. Proven
  equivalent by `tests/test_phase4_check_migrations.py`'s
  `TestAdmissionPoliciesExistParity` before the YAML file was deleted.
- **Deliberately narrower than `analyzers/compliance.py`'s
  `ComplianceAnalyzer`**, which accepts `Policy`/`ClusterPolicy`/
  `ConstraintTemplate` all three for its own separate scoring. This check
  only matches the namespaced `Policy` kind (never `ClusterPolicy`)
  because it scans the *target app's own repo*
  (`check_engine.py`'s `iter_yaml_files(repo_path)`), not the live
  cluster, and must match what `skills/compliance/kyverno-policies.md`/
  `skills/compliance/image-registry-policy.md` actually generate for that
  app: a namespace-scoped `Policy`, never a cluster-scoped
  `ClusterPolicy` (see those skills' own frontmatter/templates). Kept
  exactly this narrow during the port, per this phase's own explicit
  instruction not to silently broaden a mixed-analyzer check's scope to
  match its analyzer counterpart.

## Verification
- `kubectl get policy -n <namespace>` (Kyverno) shows at least one
  namespaced `Policy` object generated from this app's repo.
