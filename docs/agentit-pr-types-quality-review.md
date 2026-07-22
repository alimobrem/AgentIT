# AgentIT PR types — quality review

**Status:** inventory + grades (2026-07-21); **implementation in progress** (see completion matrix)  
**Repo:** AgentIT on `origin/main` (+ recent delivery / `github_pr` / `auto_delivery` / scout / skill-activate paths)  
**Companion:** [plan-quality-helpful-prs.md](./plan-quality-helpful-prs.md), [architecture-agentit-vs-fleet-gitops.md](./architecture-agentit-vs-fleet-gitops.md), [unified-apply-flow.md](./unified-apply-flow.md)

**Product contract:** Assess detects. Scan (or Capabilities / scout) generates. Humans merge on GitHub. Argo deploys. Never auto-merge. Skills are not `approved` on PR open.

---

## Completion matrix (vs recommended sequencing)

| Priority | Recommendation | Status | Where / PR |
| -------- | -------------- | ------ | ---------- |
| **P0** | Chart-aware HPA correctness (Rollout name/kind, RWO maxReplicas); fail closed | **Merged** | `portal/self_managed_hpa.py` + SkillEngine + skill template — [#136](https://github.com/alimobrem/AgentIT/pull/136); #134 closed |
| **P0** | Fleet HPA scaleTargetRef vs live Deployments/Rollouts; fail closed | **This PR** | `portal/fleet_hpa.py` + SkillEngine + auto_delivery + GitOps deliver; closed bad pinky gitops #18 |
| **P0/P1 enabler** | SSA dry-run soft-fail Forbidden / missing optional CRD | **Merged** | `classify_dry_run_error` — [#137](https://github.com/alimobrem/AgentIT/pull/137) |
| **P1** | Fleet/pinky finding-clear proof after Scan PR merge | **Done (this PR)** | PR body “Finding-clear proof”; `finding-clear-pending` Ledger event; cicd also schedules SLO verify |
| **P2** | Kill / hard-refuse `.agentit/` Scan deliveries | **Done (this PR)** | `MECHANISM_APP_REPO_PR` refused in `deliver_with_verification` + `route_and_deliver`; `create_onboarding_pr` not called |
| **P2** | Source-patch titles by mechanism | **Done (this PR)** | `create_source_patch_pr` titles: `source-repo patch` / `Scan {cluster}: source-repo patch` |
| **P2** | Scout proposal usefulness (merge yield) | **Done (this PR)** | `check_evidence_usefulness` gate — must cite dogfood/finding/PR failure signal |
| **P2** | Shared-NS blast-radius callout in PR body | **Done (this PR)** | `_deliver_via_gitops_pr` injects note into body; `build_helpful_pr_body(shared_ns_note=…)` |
| **P3** | Activate single-file staging | **Already done** | `_persist_skill_status_change` stages only `[rel_path]` |
| **P3** | Align CLI `self-fix --create-pr` with Scan gates | **Not started** | Secondary; defer until Scan P0/P1 dogfood green |

---

## Founder snapshot

| # | PR type | Grade | Fix priority |
| - | ------- | ----- | ------------ |
| 1 | Self-managed chart remediation (Scan finding-cluster) | **Partial → Good** (with #136) | **P0** — shipped in #136 |
| 2 | Fleet gitops `apps/{app}/` Scan | **Partial → Good** (with #137 + finding-clear body) | **P1** |
| 3 | Source-patch / codechange | **Partial** | **P2** — titles tightened |
| 4 | Skill activate (draft→active) | **Good** | **P3** — already single-file |
| 5 | Capability-scout self-improve | **Partial** | **P2** — evidence-usefulness gate |
| 6 | Leftover `.agentit/` / dead paths | **Bad → Quarantined** | **P2** — refused |
| 7 | Shared-namespace / cicd | **Partial** | **P2** — body blast-radius |
| — | CLI `self-fix --create-pr` | **Partial** | **P3** — deferred |

---

## How PRs get opened (code map)

| Mechanism | Function | Typical opener |
| --------- | -------- | -------------- |
| Source-repo patch | `github_pr.create_source_patch_pr` | Scan → `auto_delivery.auto_validate_and_deliver` → `route_and_deliver` (self-managed cluster/cicd + `source_patch`) |
| Infra-repo commit | `github_pr.commit_to_infra_repo` | Same Scan path for fleet apps (`MECHANISM_INFRA_REPO_COMMIT`) |
| `.agentit/` dump | `github_pr.create_onboarding_pr` | **Refused** — `MECHANISM_APP_REPO_PR` / `CATEGORY_MANIFEST_AT_REST` no longer open PRs |
| Working-tree draft PR | `git_pr.open_draft_pr` (+ `create_branch_commit_push`) | Skill Activate/Deprecate; capability-scout; CLI `self-fix --create-pr` |

Scan is the **sole product surface** that creates remediation PRs (`#123`). Capabilities Activate and capability-scout are separate intentional PR creators.

---

## 1. Self-managed chart remediation (finding-cluster Scan)

### Trigger
Auto-Scan / onboard chain for Application **`agentit`** (`is_self_managed_application`).  
`auto_validate_and_deliver` → finding gate → `partition_by_finding_cluster` → per-cluster `route_and_deliver`.

### Target repo + paths
- **Repo:** AgentIT.git (app `repo_url`)
- **Paths:** `chart/templates/**`, `skills/**` (via `remap_self_managed_cluster_files` / cicd remap)
- **Branch:** `agentit/{app}-{cluster}` e.g. `agentit/agentit-scaling`
- **Never:** `apps/agentit/` in gitops; never rewrite `argocd/application.yaml`

### Who opens it
`portal/auto_delivery.py` → `delivery._deliver_self_managed_source_pr` → `github_pr.create_source_patch_pr`

### Quality bar today
| Gate | Applies? |
| ---- | -------- |
| Finding / score gate (Phase A) | Yes |
| Filter files to open findings | Yes |
| One PR per finding cluster (≤5 files) | Yes |
| SSA dry-run (concrete YAML) | Yes (Helm templates skip SSA; concrete remap path as designed) |
| Property checks for targeted findings | Yes |
| `#119` Helm / collision / forbidden-kind | Yes |
| Helpful PR body (Phase D) | Yes |
| No skill approve on open (Phase E) | Yes (`record_skill_approval=False`) |
| App-correctness (scaleTargetRef, labels, values) | **Yes** (`self_managed_hpa.py` — #136) |

### Recent examples
| PR | Notes |
| -- | ----- |
| **#134** | Scaling / HPA — body excellent; filtered junk listed; **wrong `scaleTargetRef.name`** (`…-agentit` vs Deployment `{{ .Release.Name }}`). Closed. |
| **#124 / #129 / #128 / #120 / #116…** | Pre–Phase A dump class: “N source-level fix(es)” grab-bags into `chart/templates/`. Closed as junk. |
| gitops **#12/#16/#17** | Dead-letter era: AgentIT onboard into `apps/agentit/` — destination fixed by `#105/#114`. |

### Grade: **Good** (with #136)
**Why:** Pipe is the right shape; #134-class semantic mismatch now fail-closed (Rollout + `{{ .Release.Name }}` + RWO maxReplicas).

### Fix priority: **P0 — done in #136**

---

## 2. Fleet gitops `apps/{app}/` Scan

### Trigger
Same Scan → `auto_validate_and_deliver` for a GitOps-registered non-self-managed app (e.g. **pinky**).

### Target repo + paths
- **Repo:** `report.infra_repo_url` (typically `agentit-gitops`)
- **Paths:** `apps/{app}/{category}/{filename}`
- **Branch:** `agentit/{app}` or `agentit/{app}-{cluster}` when Phase B suffix present
- AppSet `agentit-managed-apps` syncs after merge

### Who opens it
`delivery._deliver_via_gitops_pr` → `github_pr.commit_to_infra_repo` (+ `ensure_applicationset`)

### Quality bar today
Same A–E helpers as self-managed (`quality_prs` + `pr_context` on infra commits — Phase F).  
Plus: content-unchanged skip (`_infra_repo_content_unchanged`), placeholder / Secret refuse, no Direct Apply.  
Plus: hard/soft SSA dry-run (#137); finding-clear proof section + Ledger `finding-clear-pending`.

### Recent examples
| PR (agentit-gitops) | Notes |
| ------------------- | ----- |
| **#11, #10, #9, #4, #3** | Onboard pinky — merged; early dogfood |
| **#7** | pinky SCC placeholder removal — useful surgical fix |
| **#12, #14, #16, #17** | Onboard **agentit** under `apps/agentit/` — dead letters; closed/cleaned (`#15` removed dead tree) |

### Grade: **Partial → Good** (code bar); live pinky finding-clear dogfood still the acceptance proof
**Why:** Destination and Phase A–D parity exist; soft dry-run unblocks pinky Forbidden/Kyverno; post-merge correlate path is explicit in body + events. Live “one green pinky finding-clear after merge” remains an ops dogfood step.

### Fix priority: **P1 — code done; dogfood pending**

---

## 3. Source-patch / codechange

### Trigger
Generated files classified as `CATEGORY_SOURCE_PATCH` (`category == "codechange"` or `skills/**` markdown). Routed with `MECHANISM_SOURCE_REPO_PR`.

### Target repo + paths
- **Repo:** app `repo_url`
- **Paths:** real `target_path` (Dockerfile, source files, or remapped `skills/` / chart for self-managed)
- **Branch:** default `agentit/codechange` or cluster-suffixed from Scan

### Who opens it
`deliver_with_verification` → `create_source_patch_pr`  
(Skill markdown improvements for AgentIT also land here as source patches.)

### Quality bar today
- Real path patch (not `.agentit/codechange/` copies) — fixed vs old taxonomy
- Chart paths still hit `#119` if they land under `chart/`
- Scan path gets finding filter / body when opened via `auto_delivery`
- Titles distinguish `source-repo patch` vs Scan cluster labels

### Grade: **Partial**
**Why:** Mechanism correct; titles no longer look like chart dump “N source-level fix(es)”. Volume of true Dockerfile/`src` patches still sparse.

### Fix priority: **P2 — titles done**

---

## 4. Skill activate (draft → active markdown)

### Trigger
Human **Activate** / **Reactivate** / **Deprecate** on Capabilities UI (`POST /capabilities/skills/activate` etc.), or CLI `activate-skill`.

### Target repo + paths
- **Repo:** AgentIT.git (working tree)
- **Paths:** `skills/**/*.md` — flip `status:` field
- **Branch:** `agentit/activate-skill/{stem}-{ts}` or `agentit/deprecate-skill/…`
- **Draft PR** via `git_pr.open_draft_pr`

### Who opens it
`portal/routes/capabilities.py` → `_persist_skill_status_change` → `git_pr.create_branch_commit_push` + `open_draft_pr`

### Quality bar today
- `verify_skill()` before status flip
- Never direct commit to `main`
- Body explains bake-into-image durability
- Pod flips immediately; PR survives redeploy
- Stages **only** the single skill `rel_path` (P3 hygiene already met)

### Grade: **Good**

### Fix priority: **P3 — already satisfied**

---

## 5. Capability-scout self-improve

### Trigger
Watcher / CLI `propose-once` / `propose-watch` / portal “Run self-improvement”.

### Target repo + paths
- **Repo:** AgentIT.git
- **Paths (allowlist):** `src/agentit/`, `skills/`, `checks/`, `tests/`, `docs/` (v1 often `docs/proposals/*.md` + small source)
- **Branch:** `agentit/self-improve/<slug>-<date>`
- **Draft PR**

### Who opens it
`capability_scout.py` → safety gates → `git_pr` open

### Quality bar today (fail-closed)
1. Diff size (≤3 files, ≤150 lines)  
2. Scope allowlist  
3. Secret scan  
4. Test plan required  
5. **Evidence usefulness** (cite dogfood / finding / PR failure signal)  
6. `py_compile` on touched `.py`  
7. Cap open `agentit/self-improve/*` PRs (default 1)  
8. Full pytest suite like CI  

### Grade: **Partial**
**Why:** Gates stronger; merge yield still depends on proposal quality — usefulness gate cuts speculative fluff.

### Fix priority: **P2 — gate done**

---

## 6. Leftover `.agentit/` and dead paths

### What still exists
| Path | Status |
| ---- | ------ |
| `create_onboarding_pr` → `.agentit/{category}/` | **Callable but refused** by `route_and_deliver` / `deliver_with_verification` |
| `create_agent_prs` / Per-Agent product | **Removed** (`#126`) |
| Direct Apply | **Removed** as live mechanism |
| AgentIT → `apps/agentit/` infra commit | **Hard refuse** in `commit_to_infra_repo` |
| Onboard rewrite of `argocd/application.yaml` | **Dropped** in cicd remap (`#114` / #109 class) |
| Commit / Per-Agent CTAs on Onboard Results | **Removed** (`#123`) |

### Grade: **Quarantined** (was Bad)
**Why:** Mechanism kept for historical helpers/tests; Scan path hard-refuses opening dump PRs.

### Fix priority: **P2 — done**

---

## 7. Shared-namespace / cicd

### Trigger
`classify_file` → `CATEGORY_CICD_SHARED_NAMESPACE` when manifest `metadata.namespace` ∈ operator namespaces (`_OPERATOR_NAMESPACES`).

### Target repo + paths
| App | Mechanism | Paths |
| --- | --------- | ----- |
| Fleet | `MECHANISM_INFRA_REPO_COMMIT` | `apps/{app}/cicd-shared-namespace/*` (distinct branch/prefix vs cluster-config) |
| Self-managed AgentIT | `MECHANISM_SOURCE_REPO_PR` | `chart/templates/tekton/*` or `chart/templates/*`; **Application kind dropped** |

### Quality bar today
Same Scan gates; elevated-review copy in confirmation text **and PR body**; finding-clear pending event; never reopen gitops path for AgentIT.

### Grade: **Partial → Good** (body callout)

### Fix priority: **P2 — done**

---

## Bonus: CLI `self-fix --create-pr`

### Grade: **Partial**
Shares remediation generation with Scan but **bypasses** `auto_delivery` finding-cluster / Phase D body. Secondary; do not invest until Scan P0 correctness lands.

### Fix priority: **P3 — deferred**

---

## Cross-cutting quality truths

1. **#124-class** = destination + Helm shape OK, **need** and **scope** bad → Phases A–B largely address this in code; historical PRs closed.
2. **#134-class** = need + scope + body + gates OK, **app-correctness** bad → **gated** by `self_managed_hpa.py` (#136).
3. **Dead-letter class** = wrong repo (`apps/agentit/`) → fixed in routing; keep refuse tests forever.
4. **Activate** is the cleanest PR type today; **scout** has strong gates + usefulness filter.
5. **`.agentit/`** refused on Scan path — treat remaining helper as debt to delete later.

---

## Recommended sequencing (historical)

| Priority | Work |
| -------- | ---- |
| **P0** | Chart-aware generation / validation for self-managed — **#136** |
| **P1** | Soft dry-run + finding-clear proof — **#137** + this PR |
| **P2** | Refuse `.agentit/`; scout usefulness; cicd body; source titles — **this PR** |
| **P3** | Activate single-file (already); CLI `self-fix` align — deferred |

---

*Generated from code on `origin/main` and GitHub PR history for `alimobrem/AgentIT` + `alimobrem/agentit-gitops` as of 2026-07-21; completion matrix updated same day.*
