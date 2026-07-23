---
name: ci-pipeline-exists
domain: cicd
version: 2
mode: detect
triggers: []
outputs: []
severity: high
category: pipeline
description: No CI/CD pipeline configuration found
recommendation: Create CI (GitHub Actions, GitLab CI, Jenkins, or Tekton Pipeline) for build/test/scan/deploy
rule:
  type: file_exists
  pattern:
    - ".gitlab-ci.yml"
    - "Jenkinsfile"
    - "azure-pipelines.yml"
    - ".travis.yml"
    - ".circleci/config.yml"
    - ".github/workflows/*.yml"
    - ".github/workflows/*.yaml"
    - ".tekton/*"
status: active
source: manual
---

# CI Pipeline Exists Check

## Property
Every application should have a CI pipeline definition committed to its
own repo so builds/tests/scans/deploys are reproducible.

## Rule
Fires unless the repo contains any of: `.gitlab-ci.yml`, `Jenkinsfile`,
Azure/Travis/Circle config, `.github/workflows/*.{yml,yaml}`, or
`.tekton/*`. Aligns with `CICDAnalyzer` CI path detection (not GitLab-only).

## Constraints
- Detection-only skill (`mode: detect`).
- When CI exists but is not Tekton, the analyzer emits
  `tekton_migration` (detect-only / no auto-PR) — Scan must **not** force
  a Tekton Pipeline onto healthy GHA repos.
- Remediation for **no CI at all**: `tekton-pipeline` (`pipeline` contract).

## Verification
- `.github/workflows/` or `.gitlab-ci.yml` or `.tekton/` present
- Re-Assess: high-sev `pipeline` finding resolved
