# Checks & remediations — intent audit

**Purpose:** For every Assess finding category (and major detect-only check) that can lead to remediation — or that humans care about even when Scan must not open a PR — document human intent vs detect rule vs skill/contract vs clear-evidence, with before/after dogfood evidence so you can verify yourself.

**Sources of truth (tip = `origin/main` @ post-intent-fix PR):**

| Source | Path |
| --- | --- |
| Solution contracts | `src/agentit/remediation/registry.py` → `SOLUTION_CONTRACTS` / `FIX_REGISTRY` |
| Clear-evidence | `src/agentit/remediation/clear_evidence.py` |
| Analyzers | `src/agentit/analyzers/*.py` |
| Detect + remediate skills | `skills/**` |
| Portal catalog | `src/agentit/portal/check_catalog.py` |
| Recent remediations | CHANGELOG + PRs [#195](https://github.com/alimobrem/AgentIT/pull/195)–[#203+](https://github.com/alimobrem/AgentIT/pull/203) |

**Status legend**

| Status | Meaning |
| --- | --- |
| **Fixed on tip** | Intent ≈ skill + clear-evidence on `main` (post-#195–#203+) |
| **Still wrong** | Intent ≠ skill/detect on tip — fix still needed |
| **Partial** | Right layer/skill class, but subtypes or companions still mismatch |
| **Detect-only OK** | Correctly `auto_pr=False` / human-only |
| **WIP proposed** | Correct After exists on a working branch (not merged) |

**Match:** `YES` | `PARTIAL` | `NO` — does today's Scan skill/contract match what a platform engineer would want?

Interactive twin: Cursor Canvas `checks-remediations-intent-audit.canvas.tsx` (workspace canvases folder).

---

## Summary table

| Category | Intent (1 line) | Detect (how) | Skill today (tip) | Match | Before (bad/shallow) | After (proposed or shipped) | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `container` / `dockerfile` | Pin insecure `:latest` / harden Containerfile for *that* file | Analyzer: Dockerfile* / Containerfile* (`:latest`, USER, HEALTHCHECK, non-UBI, missing) | `containerfile` · source · `dockerfile_pin` | YES | pulse-agent **#2**: pin overclaim; HEALTHCHECK/USER refused forever | Path-bound pin (#201) + additive USER/HEALTHCHECK/UBI harden on finding path | **Fixed on tip** |
| `base_image` | Replace bad base ref in source | Sentinel (`patch_base_image`) | `patch_base_image` · source · `base_image_pin` | YES | Destructive Containerfile stub (#165) | Pin-only + refuse destructive rewrite | Fixed on tip |
| `network` | Isolate workload with NetworkPolicy | Analyzer: YAML tree has no `NetworkPolicy` (if any YAML); detect `network-policy-exists` | `network-policy` · cluster · `cluster_kind` | YES | Catalog dumps / wrong-layer companions | Finding-gated + kind evidence | Fixed on tip |
| `scanning` / `vulnerability` / `cve` | CI runs a real image/dep scanner | Analyzer: no trivy/grype/snyk/… in workflows; detect `vulnerability-scanning-in-ci` | `image-scan-task` · cluster · `image_scan_task` | YES | Empty Task / `:latest` step images; detect misnamed “secrets” | #200 evidence harden; detect skill renamed | Fixed on tip |
| `image_signing` | Cosign-sign built images in CI/Tekton | Detect `image-signing-exists` (`file_contains: cosign`) | `cosign-sign-task` · cluster · `cosign_sign_task` | YES | SLSA L3 / hermetic / Konflux prose without `cosign sign` | Refuse theater; real Task (#190) | Fixed on tip |
| `resource` / `resources` | Containers have requests/limits | Analyzer: k8s YAML without resources | `resource-limits` · cluster · `resource_limits` | YES | Bare LimitRange theater possible | Shape check for requests/limits or LimitRange | Fixed on tip |
| `rbac` | Workload has SA/Role/RoleBinding | Analyzer: YAML exists but no SA/Role/RoleBinding | `rbac` · cluster · `cluster_kind` | YES | Orphan contract (no analyzer emit) | Analyzer emit + existing skill | **Fixed on tip** |
| `secrets` | Rotate leak + move to ESO/Vault | Analyzer regex + LLM classify | `__detect_only_secrets__` · none · `detect_only` | YES | Auto-PR would invent secrets | Detect-only — human rotates | Detect-only OK |
| `policy` | Admission policy for labels/limits/images | Analyzer: no Policy/ClusterPolicy/ConstraintTemplate; detect `admission-policies-exist` | `kyverno-require-labels` · cluster · `cluster_kind` | YES* | Wrong skill via triggers; companions | Honest clear_evidence (labels-only) | **Fixed on tip** (*labels-only skill; text honest) |
| `sbom` | **CI generates** SBOM every build | Analyzer `repo_has_ci_sbom_generation`; detect `file_contains` anchore/sbom-generate | `sbom-ci` · source · `sbom_ci` | YES | Static `sbom.cdx.json` / bare `sbom-task` | CI generation skill + evidence; refuse artifact/task | **Fixed on tip** |
| `audit` | App audit module **wired** into handlers | Packaged `audit.*` + import/call site (not root orphan) | `app-audit-logging` · source · `audit_wired` | YES | pinky **#8** root `audit.py`; pinky **#12** theater | Package + structure + callsite; refuse `audit-policy` | Fixed on tip |
| `license` | LICENSE file present | `LICENSE*` exists | detect-only `license-file-exists` | YES | N/A (no auto PR) | Human adds license | Detect-only OK |
| `pipeline` | Reproducible CI (any CI; Tekton when none) | Missing CI paths; detect `ci-pipeline-exists` (GHA/GitLab/Jenkins/Tekton) | `tekton-pipeline` · cluster · `cluster_kind` | YES | GitLab-only detect; GHA repos forced Tekton | Broad detect; non-Tekton → `tekton_migration` detect-only | **Fixed on tip** |
| `tekton_migration` | Optional Tekton when other CI exists | Analyzer low-sev when CI∧¬Tekton | detect-only | YES | Was remediable `pipeline` → Tekton PR | `auto_pr=False` | Detect-only OK |
| `gitops` | Real Argo Application with source | Analyzer: argoproj + `kind: Application`; detect `argocd-application-exists` | `argocd-application` · cluster · `argocd_application` | YES | Empty Application / bogus `deploy/` | #200: repoURL + path/chart + tree check | Fixed on tip |
| `metrics` / `monitoring` | Scrapable metrics via ServiceMonitor | Substring patterns / SM kind | `service-monitor` · cluster · `selector_target` | YES | Zero-match selector theater | Live label match (#200) | Fixed on tip |
| `tracing` | App emits traces | Substring jaeger/zipkin/tempo/trace | detect-only (align instrumentation) | YES | Collector YAML without app SDK | Detect-only; otel-collector does not clear | **Fixed on tip** |
| `dashboards` | Useful Grafana dashboard | grafana/dashboard substrings | `grafana-dashboard` · cluster · `grafana_dashboard` | YES | Empty panels shell | Label + non-empty panels (#200) | Fixed on tip |
| `alerting` | PrometheusRule / alert route | alertmanager/prometheusrule substrings | `alerting-rules` · cluster · `cluster_kind` | YES | Kind-only stubs | Kind evidence + skill template | Fixed on tip |
| `logging` | Structured logging library in app | structlog/zap/… substrings | detect-only `structured-logging-detected` | YES | Auto wiring would be theater | Human wires logging | Detect-only OK |
| `instrumentation` | OTel SDK in app | otel substrings | detect-only | YES | Cluster collector as fake clear | Detect-only | Detect-only OK |
| `scaling` / `autoscaling` | HPA targets real Deployment/Rollout | No HPA kind; detect `hpa-exists` | `hpa` · cluster · `hpa_target` | YES | pinky gitops HPA junk / wrong target | scaleTargetRef + live resolve | Fixed on tip |
| `availability` | Survive disruption (PDB) | No PDB | `pdb` · cluster · `selector_target` | YES | PDB opened for “single replica” | PDB-only; replicas split out | **Fixed on tip** |
| `replicas` | Multi-replica redundancy | replicas&lt;2 | `workload-replicas` · source · `workload_replicas` | YES | Mapped to PDB | Deployment/Rollout replica patch | **Fixed on tip** |
| `health` | Probes on containers in manifests | No liveness/readiness in YAML; detect `health-probes-check` | `workload-health-probes` · source · `workload_probes` | YES | Kyverno-only (Assess still failed) | Source YAML probe injection; Kyverno companion refused | **Fixed on tip** |
| `quota` | Namespace ResourceQuota/LimitRange | No RQ/LR when manifests exist | `resourcequota` · cluster · `quota_manifest` | YES | Capability-scout reject loops | Kind evidence + capability gate (#195) | Fixed on tip |
| `iac` / `manifests` | Real Helm/Kustomize/TF or k8s templates | No Chart/kustomize/tf / no apiVersion+kind | `helm-chart` · source · `helm_chart` | YES | Chart.yaml without templates | Chart.yaml + templates with kind | Fixed on tip |
| `eol` | Bump EOL runtime pin | `.node-version` / language EOL detectors | `eol-upgrade` · source · `runtime_pin` | YES | Theater version files | Pin with digits | Fixed on tip |
| `migration` | Real schema evolution path | No Alembic/Flyway/… **and** no hand-rolled DDL | `db-migration-tooling` · source · `migration_tooling` | YES | #157 SELECT 1 / empty upgrade | Refuse shallow DDL; skip hand-rolled SCHEMA_SQL | Fixed on tip |
| `backup` / `retention` | Human-designed backup/retention | substring backup/retention | detect-only exists skills | YES | Auto CronJob theater | Detect-only | Detect-only OK |

---

## Top mismatches (fix priority) — closed

| Rank | Category | Was | After | Status |
| ---: | --- | --- | --- | --- |
| 1 | **`sbom`** | Static artifact | `sbom-ci` + `sbom_ci` evidence; refuse task/artifact | Fixed |
| 2 | **`container` subtypes** | Refuse forever | Path-bound USER/HEALTHCHECK/UBI harden | Fixed |
| 3 | **`availability` (replicas)** | PDB for single replica | `replicas` → `workload-replicas`; PDB stays | Fixed |
| 4 | **`health`** | Kyverno-only | Source `workload-health-probes` + `workload_probes` | Fixed |
| 5 | **`pipeline` (Tekton bias)** | Force Tekton on GHA | Broad detect; `tekton_migration` detect-only | Fixed |
| 6 | **`tracing`** | otel-collector clear | Detect-only (align instrumentation) | Fixed |
| 7 | **`policy`** | Implied full pack | Honest labels-only clear_evidence | Fixed |
| 8 | **`rbac`** | Orphan contract | Analyzer emit | Fixed |

Already hardened earlier: path-bound `dockerfile_pin` (#201), shallow evidence (#200), SBOM empty-shell refuse (#199), webhook/capability honesty (#195–#198), image signing (#190), audit wire, migration shallow refuse.

---

## Tip vs WIP checklist (verify yourself)

| Claim | How to verify |
| --- | --- |
| SBOM clears via CI | `rg '"sbom"' -A6 src/agentit/remediation/registry.py` → `sbom-ci` / `SBOM_CI` |
| Detect SBOM | `head skills/compliance/sbom-exists.md` → `file_contains` anchore/sbom-generate |
| Container subtypes clear | `tests/test_clear_evidence.py` HEALTHCHECK/USER/UBI allow cases; `harden_dockerfile_content` |
| Replicas vs PDB | `ha_dr.py` emits `replicas` + `availability`; contracts `workload-replicas` / `pdb` |
| Health source probes | `contract_for("health").skill_name == "workload-health-probes"` |
| Pipeline GHA | `skills/cicd/ci-pipeline-exists.md` patterns include `.github/workflows/*` |
| Tracing detect-only | `allows_auto_pr("tracing") is False` |
| Scanning detect rename | `skills/security/vulnerability-scanning-in-ci.md` |

---

## Related PRs (195–203+ + dogfood)

| PR | Relevance |
| --- | --- |
| [#203+](https://github.com/alimobrem/AgentIT/pull/203) | Intent-audit fixes: SBOM→CI + remaining mismatches |
| [#201](https://github.com/alimobrem/AgentIT/pull/201) | `dockerfile_pin` path-bound (pulse-agent#2) |
| [#200](https://github.com/alimobrem/AgentIT/pull/200) | Shallow Scan PR evidence harden |
| [#199](https://github.com/alimobrem/AgentIT/pull/199) | SBOM components inventory (still static path; demoted) |
| [#198](https://github.com/alimobrem/AgentIT/pull/198) | Webhook → gated auto_delivery |
| [#196](https://github.com/alimobrem/AgentIT/pull/196) | Async webhook claims |
| [#195](https://github.com/alimobrem/AgentIT/pull/195) | Capability SSA honesty — never block source PRs |
| [#190](https://github.com/alimobrem/AgentIT/pull/190) | Image signing → cosign-sign-task |
| [#154](https://github.com/alimobrem/AgentIT/pull/154) / [#158](https://github.com/alimobrem/AgentIT/pull/158) | SOLUTION_CONTRACTS + clear-evidence |

Dogfood narrative: [`docs/history/changelog-dogfood-notes.md`](./history/changelog-dogfood-notes.md) (historical; this audit is current product truth for intent vs skill).

---

## Out of scope (this pass)

- Broader policy pack beyond labels-only Kyverno (honest text shipped instead).
- Full Capabilities catalog UI redesign.
- App OpenTelemetry SDK auto-instrumentation skill (tracing stays detect-only).
