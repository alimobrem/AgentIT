# Checks & remediations — intent audit

**Purpose:** For every Assess finding category (and major detect-only check) that can lead to remediation — or that humans care about even when Scan must not open a PR — document human intent vs detect rule vs skill/contract vs clear-evidence, with before/after dogfood evidence so you can verify yourself.

**Sources of truth (tip = `origin/main` @ `#201`):**

| Source | Path |
| --- | --- |
| Solution contracts | `src/agentit/remediation/registry.py` → `SOLUTION_CONTRACTS` / `FIX_REGISTRY` |
| Clear-evidence | `src/agentit/remediation/clear_evidence.py` |
| Analyzers | `src/agentit/analyzers/*.py` |
| Detect + remediate skills | `skills/**` |
| Portal catalog | `src/agentit/portal/check_catalog.py` |
| Recent remediations | CHANGELOG + PRs [#195](https://github.com/alimobrem/AgentIT/pull/195)–[#201](https://github.com/alimobrem/AgentIT/pull/201) |

**Status legend**

| Status | Meaning |
| --- | --- |
| **Fixed on tip** | Intent ≈ skill + clear-evidence on `main` (post-#195–#201) |
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
| `container` / `dockerfile` | Pin insecure `:latest` / harden Containerfile for *that* file | Analyzer: Dockerfile* / Containerfile* (`:latest`, USER, HEALTHCHECK, non-UBI, missing) | `containerfile` · source · `dockerfile_pin` | PARTIAL | pulse-agent **#2**: pin `Dockerfile` claimed clear for `Dockerfile.deps`/`.fast`; HEALTHCHECK/USER cleared by pin theater | Path-bound pin (#201); refuse HEALTHCHECK/USER/non-UBI via pin alone; greenfield template still ships USER+HEALTHCHECK | Fixed path-overclaim; **PARTIAL** for USER/HEALTHCHECK/UBI subtypes |
| `base_image` | Replace bad base ref in source | Sentinel (`patch_base_image`) | `patch_base_image` · source · `base_image_pin` | YES | Destructive Containerfile stub (#165) | Pin-only + refuse destructive rewrite | Fixed on tip |
| `network` | Isolate workload with NetworkPolicy | Analyzer: YAML tree has no `NetworkPolicy` (if any YAML); detect `network-policy-exists` | `network-policy` · cluster · `cluster_kind` | YES | Catalog dumps / wrong-layer companions | Finding-gated + kind evidence | Fixed on tip |
| `scanning` / `vulnerability` / `cve` | CI runs a real image/dep scanner | Analyzer: no trivy/grype/snyk/… in workflows; detect `secrets-scanning-in-ci` (name drift) | `image-scan-task` · cluster · `image_scan_task` | YES* | Empty Task / `:latest` step images (skills audit) | #200: require trivy\|grype\|snyk + pinned images | Fixed on tip (*detect skill name still says “secrets”) |
| `image_signing` | Cosign-sign built images in CI/Tekton | Detect `image-signing-exists` (`file_contains: cosign`) | `cosign-sign-task` · cluster · `cosign_sign_task` | YES | SLSA L3 / hermetic / Konflux prose without `cosign sign` | Refuse theater; real Task (#190) | Fixed on tip |
| `resource` / `resources` | Containers have requests/limits | Analyzer: k8s YAML without resources | `resource-limits` · cluster · `resource_limits` | YES | Bare LimitRange theater possible | Shape check for requests/limits or LimitRange | Fixed on tip |
| `rbac` | Workload has SA/Role/RoleBinding | Contract alias (no dedicated analyzer emit today) | `rbac` · cluster · `cluster_kind` | PARTIAL | Keyword false-matches historically | Contracted; weak detect path | Partial (orphan contract vs analyzer) |
| `secrets` | Rotate leak + move to ESO/Vault | Analyzer regex + LLM classify | `__detect_only_secrets__` · none · `detect_only` | YES | Auto-PR would invent secrets | Detect-only — human rotates | Detect-only OK |
| `policy` | Admission policy for labels/limits/images | Analyzer: no Policy/ClusterPolicy/ConstraintTemplate; detect `admission-policies-exist` | `kyverno-require-labels` · cluster · `cluster_kind` | PARTIAL | Wrong skill via triggers; companions `image-registry-policy` | Registry exact map + refuse companions | Partial (labels-only vs full recommendation) |
| `sbom` | **CI generates** SBOM every build | Tip: `file_exists *sbom*` + analyzer file scan; WIP: CI generation | Tip: `sbom-artifact` · source · `sbom_file` | **NO** | Cluster `sbom-task` never cleared Assess; empty `components: []` (pulse-agent **#3**); static file ≠ CI | Tip #199 fills components; **product After:** `sbom-ci` + `sbom_ci` evidence (WIP `fix/sbom-ci-generation`) | **Still wrong on tip**; WIP proposed |
| `audit` | App audit module **wired** into handlers | Packaged `audit.*` + import/call site (not root orphan) | `app-audit-logging` · source · `audit_wired` | YES | pinky **#8** root `audit.py`; pinky **#12** theater + middleware launder; apiserver `audit-policy` false clear | Package + structure + callsite; refuse `audit-policy` companion | Fixed on tip |
| `license` | LICENSE file present | `LICENSE*` exists | detect-only `license-file-exists` | YES | N/A (no auto PR) | Human adds license | Detect-only OK |
| `pipeline` | Reproducible CI (ideally Tekton on OCP) | Missing CI paths / Tekton; detect `ci-pipeline-exists` (**.gitlab-ci.yml only**) | `tekton-pipeline` · cluster · `cluster_kind` | PARTIAL | Hello-World catalog dumps; “has GHA” still nagged for Tekton | Finding gate; low-sev Tekton migration still pushes Tekton | Partial (detect skill narrow; Tekton bias) |
| `gitops` | Real Argo Application with source | Analyzer: argoproj + `kind: Application`; detect `argocd-application-exists` | `argocd-application` · cluster · `argocd_application` | YES | Empty Application / bogus `deploy/` | #200: repoURL + path/chart + tree check | Fixed on tip |
| `metrics` / `monitoring` | Scrapable metrics via ServiceMonitor | Substring patterns / SM kind | `service-monitor` · cluster · `selector_target` | YES | Zero-match selector theater | Live label match (#200) | Fixed on tip |
| `tracing` | App emits traces | Substring jaeger/zipkin/tempo/trace | `otel-collector` · cluster · `cluster_kind` | PARTIAL | Collector YAML without app SDK | Prefer detect-only or app instrumentation skill | Partial |
| `dashboards` | Useful Grafana dashboard | grafana/dashboard substrings | `grafana-dashboard` · cluster · `grafana_dashboard` | YES | Empty panels shell | Label + non-empty panels (#200) | Fixed on tip |
| `alerting` | PrometheusRule / alert route | alertmanager/prometheusrule substrings | `alerting-rules` · cluster · `cluster_kind` | YES | Kind-only stubs | Kind evidence + skill template | Fixed on tip (lighter bar than dashboards) |
| `logging` | Structured logging library in app | structlog/zap/… substrings | detect-only `structured-logging-detected` | YES | Auto wiring would be theater | Human wires logging | Detect-only OK |
| `instrumentation` | OTel SDK in app | otel substrings | detect-only (same skill name as logging contract) | YES | Cluster collector as fake clear | Detect-only | Detect-only OK |
| `scaling` / `autoscaling` | HPA targets real Deployment/Rollout | No HPA kind; detect `hpa-exists` | `hpa` · cluster · `hpa_target` | YES | pinky gitops HPA junk / wrong target | scaleTargetRef + live resolve | Fixed on tip |
| `availability` | Survive disruption **and** multi-replica | No PDB **or** replicas&lt;2 | `pdb` · cluster · `selector_target` | PARTIAL | `pod-delete` chaos won keyword race; PDB opened for “single replica” | PDB for PDB finding; refuse `pod-delete`; **replicas still uncovered** | Partial (replica finding) |
| `health` | Probes on containers in manifests | No liveness/readiness in YAML; detect `health-probes-check` | `health-probes-policy` · cluster · Kyverno mutate | PARTIAL | Policy without probes in repo still fails Assess | Prefer patch Deployment probes / chart values | Partial |
| `quota` | Namespace ResourceQuota/LimitRange | No RQ/LR when manifests exist | `resourcequota` · cluster · `quota_manifest` | YES | Capability-scout reject loops | Kind evidence + capability gate (#195) | Fixed on tip |
| `iac` / `manifests` | Real Helm/Kustomize/TF or k8s templates | No Chart/kustomize/tf / no apiVersion+kind | `helm-chart` · source · `helm_chart` | YES | Chart.yaml without templates | Chart.yaml + templates with kind | Fixed on tip |
| `eol` | Bump EOL runtime pin | `.node-version` / language EOL detectors | `eol-upgrade` · source · `runtime_pin` | YES | Theater version files | Pin with digits | Fixed on tip |
| `migration` | Real schema evolution path | No Alembic/Flyway/… **and** no hand-rolled DDL | `db-migration-tooling` · source · `migration_tooling` | YES | #157 SELECT 1 / empty upgrade; AgentIT false-positive Alembic | Refuse shallow DDL; skip hand-rolled SCHEMA_SQL | Fixed on tip |
| `backup` / `retention` | Human-designed backup/retention | substring backup/retention | detect-only exists skills | YES | Auto CronJob theater | Detect-only | Detect-only OK |

\*Scanning detect skill filename/description still say “secrets” while category is `scanning` — catalog confusion only.

---

## Top mismatches (fix priority)

Ranked by **intent ≠ skill** severity × dogfood pain × how wrong Merge would look.

| Rank | Category | Match | Why | Tip status | Recommended next |
| ---: | --- | --- | --- | --- | --- |
| 1 | **`sbom`** | NO | Platform eng wants **CI generation**; tip still remediates with static `sbom.cdx.json` (`sbom-artifact` / `sbom_file`). #199 fixed empty shells but not the product path. | Still wrong | Land `sbom-ci` + analyzer `repo_has_ci_sbom_generation` + clear-evidence `sbom_ci` (WIP `fix/sbom-ci-generation`); demote artifact & bare task to companions |
| 2 | **`container` subtypes** | PARTIAL | `:latest` path-bound is fixed (#201), but USER / HEALTHCHECK / non-UBI still share `dockerfile_pin` and **refuse** (no clearing skill) — Scan correctly won't open a lie, but findings stay open forever | Partial | Dedicated patches: add USER / HEALTHCHECK / UBI FROM on the **finding path**, or split categories |
| 3 | **`availability` (replicas)** | PARTIAL | “Single replica” finding maps to **PDB** skill — wrong fix | Partial | Separate category `replicas` → Deployment replica patch / chart values; keep `availability`→PDB for PDB-only |
| 4 | **`health`** | PARTIAL | Assess looks for probes **in repo YAML**; remediation is Kyverno **mutate** Policy — MERGE may not clear Assess until live mutation + re-render | Partial | Source/chart probe injection skill; keep Kyverno as optional companion |
| 5 | **`pipeline` (Tekton bias)** | PARTIAL | Low-sev “CI exists but not Tekton” + detect skill only sees `.gitlab-ci.yml` | Partial | Narrow detect to analyzer parity; demote Tekton migration to detect-only or optional |
| 6 | **`tracing`** | PARTIAL | Cluster OTel collector ≠ app instrumentation | Partial | Align with `instrumentation` detect-only, or source SDK skill |
| 7 | **`policy`** | PARTIAL | Recommendation cites limits/labels/images; skill is labels-only Kyverno | Partial | Broader policy pack or honest clear_evidence text |
| 8 | **`rbac` / aliases** | PARTIAL | Contracted without analyzer emission | Partial | Emit analyzer finding or drop unused contract |

Already hardened on tip (do not re-litigate as P0): path-bound `dockerfile_pin` (#201), shallow evidence kinds (#200), SBOM empty-shell refuse (#199), webhook/capability honesty (#195–#198), image signing (#190), audit wire, migration shallow refuse.

---

## Detailed sections

### Security

#### `container` / `dockerfile`

1. **Intent** — Pin or replace insecure container bases and harden the **specific** Dockerfile/Containerfile Assess named.
2. **How Assess detects** — `SecurityAnalyzer._check_dockerfile` / `_check_base_image` + `CICDAnalyzer` missing Dockerfile; descriptions end with ` in {path}`. Detect skills: `dockerfile-exists`, `containerfile-exists`.
3. **Remediation skill + contract** — `security/containerfile` · `delivery: source` · `evidence_kind: dockerfile_pin` · refuses `image-registry-policy`, `limitrange`, `image-scan-task`, `kyverno-require-labels`.
4. **Match?** — **PARTIAL**
5. **Gap** — Pin-only clears `:latest`. USER / HEALTHCHECK / non-UBI are correctly **refused** by simulation but have no alternate clearing skill.
6. **Before** — pulse-agent **#2**: pinning root `Dockerfile` over-claimed clear for `Dockerfile.deps` / `.fast`; destructive stub rewrite (#165).
7. **After** — **Shipped #201:** path-bound pin + subtype refuse. **Still needed:** path-bound USER/HEALTHCHECK/UBI patches.

#### `network`

1. **Intent** — Default-deny NetworkPolicy with explicit allows for the app.
2. **Detect** — No `NetworkPolicy` in YAML when YAML exists; detect `network-policy-exists` (`yaml_kind_exists`).
3. **Skill** — `network-policy` · cluster · `cluster_kind` (NetworkPolicy).
4. **Match?** — **YES**
5. **Gap** — None material (selector quality lighter than PDB/SM).
6. **Before** — Wrong-layer / catalog companions on early dogfood.
7. **After** — Solution contracts + finding gate (#154+).

#### `scanning` (+ contract aliases `vulnerability`, `cve`)

1. **Intent** — CI/Tekton runs a real scanner before ship.
2. **Detect** — Analyzer scans workflows for scanner names; detect skill `secrets-scanning-in-ci` (`file_contains: trivy`) — **misnamed**.
3. **Skill** — `image-scan-task` · cluster · `image_scan_task`.
4. **Match?** — **YES** (skill); detect naming **PARTIAL**.
5. **Gap** — Rename detect skill; optionally accept GHA-only Trivy as clear without Tekton Task.
6. **Before** — Empty Task / `:latest` images (skills audit).
7. **After** — **#200** evidence harden.

#### `image_signing`

1. **Intent** — Cosign sign/attest in the build pipeline.
2. **Detect** — `image-signing-exists` (`file_contains: cosign`).
3. **Skill** — `cosign-sign-task` · cluster · `cosign_sign_task`.
4. **Match?** — **YES**
5. **Gap** — None for good-PR path.
6. **Before** — SLSA/hermetic theater PRs.
7. **After** — **#190** + clear-evidence refuse.

#### `secrets`

1. **Intent** — Humans rotate leaked credentials and adopt ESO/Vault.
2. **Detect** — Regex + LLM classify + cache.
3. **Skill** — Detect-only sentinel; `auto_pr=False`.
4. **Match?** — **YES**
5. **Gap** — None (correctly no Scan PR).
6. **Before** — FP floods (placeholders / alert label names) — mitigated by classify heuristics.
7. **After** — Detect-only + FP filters (CHANGELOG Unreleased).

### Compliance

#### `sbom` — **P0 mismatch**

1. **Intent** — Every build **generates** an SBOM in CI (GHA `anchore/sbom-action` / Syft / Tekton Pipeline wire).
2. **How Assess detects (tip)** — Compliance analyzer + detect `sbom-exists`: **`file_exists` pattern `*sbom*`** (static file).
3. **Remediation (tip)** — `sbom-artifact` · source · `sbom_file` → commit `sbom.cdx.json`.
4. **Match?** — **NO**
5. **Gap** — Static artifact ≠ CI generation; bare cluster `sbom-task` is wrong-layer.
6. **Before** — Tekton `sbom-task` PRs that never cleared Assess; pulse-agent **#3** empty `components: []`.
7. **After** — Tip **#199** populates components (still static path). **Proposed (WIP):** analyzer `repo_has_ci_sbom_generation`, skill `sbom-ci`, evidence `sbom_ci`, refuse `sbom-task`/`sbom-artifact` companions; detect skill → `file_contains` anchore/sbom-generate.

#### `audit`

1. **Intent** — Packaged audit module + real import/call on privileged paths.
2. **Detect** — Packaged `audit.*` + `has_audit_usage` elsewhere (not YAML “audit” substrings).
3. **Skill** — `app-audit-logging` · source · `audit_wired`; refuse `audit-policy`.
4. **Match?** — **YES**
5. **Gap** — None material.
6. **Before** — pinky **#8** root orphan; pinky **#12** thin theater + middleware; apiserver Policy false clear.
7. **After** — Structure markers + package path + pre-enrich before simulation.

#### `policy`

1. **Intent** — Admission controls for labels, limits, approved images.
2. **Detect** — Policy/ClusterPolicy/ConstraintTemplate; detect `admission-policies-exist`.
3. **Skill** — `kyverno-require-labels` only.
4. **Match?** — **PARTIAL**
5. **Gap** — Labels-only vs recommendation breadth.
6. **Before** — Trigger-keyword wrong skill races.
7. **After** — Exact `FIX_REGISTRY` + refuse companions.

#### `license`

Detect-only `license-file-exists` — **YES** / Detect-only OK.

### CI/CD

#### `pipeline`

1. **Intent** — Some CI exists; on OpenShift, Tekton is preferred but not mandatory for every repo.
2. **Detect** — Analyzer: workflows / Jenkins / `.tekton` / Pipeline kinds; detect skill only `.gitlab-ci.yml`.
3. **Skill** — `tekton-pipeline` · cluster.
4. **Match?** — **PARTIAL**
5. **Gap** — Detect skill under-coverage; low-sev non-Tekton finding pushes Tekton onto healthy GHA repos.
6. **Before** — Catalog blast (Hello-World #31/#32 class).
7. **After** — Finding gate + file caps; detect/skill honesty still due.

#### `gitops`

1. **Intent** — Argo Application with real `repoURL` + path/chart.
2. **Detect** — argoproj + Application kind; detect `argocd-application-exists`.
3. **Skill** — `argocd-application` · `argocd_application`.
4. **Match?** — **YES**
5. **Gap** — None material after #200.
6. **Before** — Empty Application / missing `deploy/`.
7. **After** — **#200** tree-aware refuse.

### Observability

| Category | Match | Notes |
| --- | --- | --- |
| `metrics` / `monitoring` | YES | SM + live selector (#200) |
| `dashboards` | YES | panels required (#200) |
| `alerting` | YES | lighter `cluster_kind` bar |
| `tracing` | PARTIAL | collector ≠ SDK |
| `logging` / `instrumentation` | YES | detect-only |

### HA / infrastructure / data

#### `availability`

1. **Intent** — Multi-replica **and** PDB for voluntary disruption.
2. **Detect** — replicas&lt;2 **or** no PDB (same category!).
3. **Skill** — Always `pdb`.
4. **Match?** — **PARTIAL** (PDB half YES; replica half **NO**).
5. **Before** — `pod-delete` chaos skill stole keyword match.
6. **After** — Registry → `pdb` + refuse `pod-delete`; split replica finding still open.

#### `health`

1. **Intent** — Probes in workload manifests.
2. **Skill** — Kyverno mutate Policy.
3. **Match?** — **PARTIAL** (cluster mutate may not satisfy repo Assess).

#### `scaling` / `quota` / `iac` / `manifests` / `eol` / `migration`

Match **YES** after #154–#200 hardening (HPA target, quota kinds, Helm chart shape, runtime pin, migration DDL refuse + hand-rolled skip).

#### `backup` / `retention`

Detect-only — **YES**.

---

## Tip vs WIP checklist (verify yourself)

| Claim | How to verify |
| --- | --- |
| Tip SBOM still static | `rg '"sbom"' -A6 src/agentit/remediation/registry.py` → `sbom-artifact` / `SBOM_FILE` |
| Tip detect SBOM | `head skills/compliance/sbom-exists.md` → `file_exists` `*sbom*` |
| Path-bound pin on tip | `tests/test_clear_evidence.py` pulse-agent#2 cases; CHANGELOG #201 |
| Shallow evidence on tip | CHANGELOG #200 bullets; `IMAGE_SCAN_TASK` / `GRAFANA_DASHBOARD` / `SELECTOR_TARGET` / `ARGOCD_APPLICATION` in `clear_evidence.py` |
| SBOM CI After (WIP) | Stash `wip-sbom-ci-preserve-for-audit-pr` or branch `fix/sbom-ci-generation`: skill `skills/compliance/sbom-ci.md`, evidence `SBOM_CI` |
| Pinky audit | Search `audit_wired` / pinky #8/#12 in `clear_evidence.py` + CHANGELOG |
| pulse-agent #2/#3 | CHANGELOG Fixed; tests named in `test_clear_evidence.py` / `test_sbom_build.py` |

---

## Related PRs (195–201 + dogfood)

| PR | Relevance |
| --- | --- |
| [#201](https://github.com/alimobrem/AgentIT/pull/201) | `dockerfile_pin` path-bound (pulse-agent#2) |
| [#200](https://github.com/alimobrem/AgentIT/pull/200) | Shallow Scan PR evidence harden |
| [#199](https://github.com/alimobrem/AgentIT/pull/199) | SBOM components inventory (still static path) |
| [#198](https://github.com/alimobrem/AgentIT/pull/198) | Webhook → gated auto_delivery |
| [#196](https://github.com/alimobrem/AgentIT/pull/196) | Async webhook claims |
| [#195](https://github.com/alimobrem/AgentIT/pull/195) | Capability SSA honesty — never block source PRs |
| [#190](https://github.com/alimobrem/AgentIT/pull/190) | Image signing → cosign-sign-task |
| [#154](https://github.com/alimobrem/AgentIT/pull/154) / [#158](https://github.com/alimobrem/AgentIT/pull/158) | SOLUTION_CONTRACTS + clear-evidence |

Dogfood narrative: [`docs/history/changelog-dogfood-notes.md`](./history/changelog-dogfood-notes.md) (historical; this audit is current product truth for intent vs skill).

---

## Out of scope (this pass)

- Implementing `sbom-ci` or other skill fixes (separate agent/PR).
- Renaming misnamed detect skills beyond noting them.
- Full Capabilities catalog UI redesign.
