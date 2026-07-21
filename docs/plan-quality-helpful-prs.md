# Founder plan: quality PRs that help the app

**Status:** implemented (2026-07-21) — Phases A–F landed in `feat/quality-helpful-prs`  
## Phase completion matrix

| Phase | Status | Where |
| ----- | ------ | ----- |
| **A** Finding/score gate | Done | `quality_prs.finding_gate_allows_pr` + filter in `auto_validate_and_deliver` |
| **B** One PR per finding cluster | Done | `partition_by_finding_cluster` → N× `route_and_deliver` |
| **C** Compose validation | Done | Per-cluster SSA dry-run + property checks before open |
| **D** PR body = why this helps | Done | `build_helpful_pr_body` → `create_source_patch_pr` / `commit_to_infra_repo` |
| **E** Learn from merge/reject | Done | Never approve on open; approve on finding-resolved; reject on still-present / PR close |
| **F** Fleet (pinky) parity | Done | Same Scan path for infra-repo commits under `apps/{app}/` |

What NOT to do (still enforced): no skill-pack dumps, no Per-Agent product, no auto-merge, no Direct Apply, no approve-on-PR-open.

**Audience:** founders sequencing dogfood quality after destination/gate/filter work  
**Normative companions:** [architecture-agentit-vs-fleet-gitops.md](./architecture-agentit-vs-fleet-gitops.md), [onboarding-loop-vision-gap-analysis.md](./onboarding-loop-vision-gap-analysis.md), [unified-apply-flow.md](./unified-apply-flow.md)

---

## One-line product contract

**Assess detects. Onboard generates. Humans merge on GitHub. Argo deploys.**  
A good AgentIT PR is one a human can merge without rewriting, that clears a real finding or raises score, and that does not fight Application ownership.

Destination (#105/#114), refuse-junk gate (#119), filter + generation (#121), Scan-only UI (#123), and SSA dry-run (#125) made PRs *land in the right place* and *look Helm-shaped*. They did **not** yet make PRs *help the app*. This plan closes that gap.

---

## 1. Definition of a good PR

Acceptance is measurable and differs by path. “Helm-shaped” is necessary for self-managed chart files; it is never sufficient.

### Shared (every PR AgentIT opens)

| Criterion | Pass when |
| --------- | --------- |
| **Tied to need** | PR cites ≥1 open Ledger/assessment finding key, or a measured score delta the change targets |
| **Scoped** | One finding cluster (or one intentional skill-catalog theme) — not a grab-bag of unrelated templates |
| **Reviewable** | Diff is small enough to reason about; PR body states finding → change → expected score/risk |
| **Validated before open** | SSA dry-run (concrete YAML) and/or property checks clean for in-scope findings; no chart path collisions |
| **Human merge gate** | Opens as draft or ready-for-review; **never** auto-merged |
| **Argo-only apply** | No Direct Apply; merge + Argo (or Tekton→notify-argocd→Application `agentit`) is the only deploy path |
| **Honest skill credit** | Skill outcome `approved` only after merge + evidence of help (score/finding clear), never on PR open |

### Fleet app (e.g. pinky → `agentit-gitops` `apps/{app}/`)

| Criterion | Pass when |
| --------- | --------- |
| **Right tree** | Files under `apps/{app}/…` via `commit_to_infra_repo` / `MECHANISM_INFRA_REPO_COMMIT` |
| **AppSet-compatible** | Directory-of-manifests shape AppSet `agentit-managed-apps` can sync; no forbidden ownership fight with a hand-crafted Application |
| **Finding cleared** | Post-merge re-Assess: `correlate_delivery_finding()` → `resolved` for targeted keys, or score up in the dimension(s) claimed |
| **No secret/placeholder leaks** | Existing guards still fail closed |
| **Merge hygiene** | Merged without force-push / “rewrite everything” follow-ups in the same session |

### Self-managed AgentIT (Application `agentit` → AgentIT.git)

| Criterion | Pass when |
| --------- | --------- |
| **Right repo/path** | AgentIT.git only: `skills/**`, curated `chart/templates/**` / `chart/values.yaml`, or real `src/**` — **never** `apps/agentit/` in gitops, **never** onboard rewrite of `argocd/application.yaml` |
| **Helm-shaped where chart** | Passes `validate_self_managed_chart_delivery()` (#119): Helm markers, no collision, no forbidden kinds (`PipelineRun`, `ClusterRole*`, `ClusterTask`, `Application`) |
| **Actually useful** | Prefer `skills/**` markdown that improves generation, or a chart patch that fixes a dogfood finding — not 16 unrelated template dumps |
| **Survives CI** | Merge → Tekton build/smoke → `notify-argocd` image pin → Application `agentit` syncs without clobber |
| **Score/dogfood** | Self-health / re-Assess after rollout shows finding gone or score improved; Ledger shows why |

**Anti-definition (not a good PR even if green):** Helm-shaped fluff that adds unused templates; skill pack dumps; “onboard everything” batches with no finding link; PRs that exist only because Auto-Scan always generates.

### App-correctness beyond Helm-shaped (post-#134)

`validate_self_managed_chart_delivery()` / Helm markers prove a file *looks* like a chart patch. They do **not** prove it *attaches* to the live workload. Dogfood #134 cleared the structural bar with an HPA that targeted `Deployment` / `{{ .Release.Name }}-agentit` while the chart uses an Argo Rollout named `{{ .Release.Name }}`, and set `maxReplicas: 10` against a ReadWriteOnce data PVC.

**Bar addendum for self-managed chart PRs:** scale targets (name + kind/apiVersion) must match chart workload facts (`portal/self_managed_hpa.py`); RWO-backed apps must not get multi-replica HPA lies. Prefer `needs_attention` + why over opening a PR that would clear `hpa-exists` without working.

---

## 2. Current gaps (why #124-class PRs still aren’t “help the app”)

Shipped foundation (do not re-litigate):

| Shipped | Module / mechanism |
| ------- | ------------------ |
| Destination fixed | `is_self_managed_application()`, remap + `MECHANISM_SOURCE_REPO_PR` → AgentIT.git (`delivery.py`, #105/#114) |
| Fail-closed chart gate | `validate_self_managed_chart_delivery()` (#119) |
| Filter + generate better | `filter_self_managed_delivery_files()`, `SkillEngine(self_managed=True)` (#121) |
| Scan-only PR creation | `auto_delivery.auto_validate_and_deliver()`; Onboard Results CTAs removed (#123) |
| SSA dry-run preflight | `cluster_apply.dry_run_manifests_against_cluster()` via `deliver_with_verification(dry_run=True)` (#125) |
| Finding correlation plumbing | `route_and_deliver(..., target_findings=)`, `correlate_delivery_finding()`, Ledger |

**Still broken relative to “good PR”:**

1. **Generation is still catalog-wide, not finding-gated.** Onboard / Auto-Scan still tends to generate for the skill catalog / property blanket, then filter/gate. `_assessment_has_finding_category()` already gates *auto-fix* in `auto_delivery.py`, but the first generation pass can still produce many files that are merely “allowed,” not “needed.” Result: #124-style PRs that are Helm-shaped and mergeable-looking but not tied to open findings or score delta.

2. **One PR = many unrelated templates.** `create_source_patch_pr` / infra commit batch every surviving file into one PR. There is no “cluster by finding / domain / dependency” step. Reviewers get volume instead of a story.

3. **Validation is structural, not outcome-shaped.** SSA dry-run proves the apiserver would accept concrete YAML; `property_verifier.verify_all_properties()` proves four properties exist. Neither answers “will this clear finding X / raise score?” Helm templates intentionally skip SSA. Chart collision checks prevent overwrite, not uselessness.

4. **PR body is a file list, not a causal story.** `github_pr.create_source_patch_pr` body is “source-level fixes” + paths + descriptions. Orchestrator has per-file “why” metadata, but it is not elevated to finding → change → expected score/risk. Humans cannot triage from GitHub alone.

5. **Learning still weak / wrong timing.** Self-managed correctly sets `record_skill_approval=False` on PR open (`_deliver_self_managed_source_pr`). Fleet still historically records `approved` on PR open (`deliver_with_verification` default). Merge/reject → skill weight updates are incomplete for the Scan path; capability-scout can prefer rejected categories, but onboard does not systematically avoid skills that opened junk PRs.

6. **Fleet (pinky) quality ≠ self-managed quality.** Self-managed got #119/#121 attention after the gitops dead-letter incident. Fleet still risks “lots of YAML under `apps/{app}/`” without the same finding-cluster + PR-body bar. Pinky path parity is Phase F, not free.

7. **Simplify work (parallel) is a dependency, not a distraction.** Cost/dependency/codechange still linger in the product surface; the simplify sequence collapses competing CTAs and mental models. Quality gates that assume “one Scan → one honest PR story” land cleaner after simplify removes dual paths (Per-Agent, Commit, Direct Apply nostalgia). **Do not block Phase A on full simplify**, but **do not invest in Per-Agent/Direct Apply UX** while simplify runs.

---

## 3. Phased roadmap (order + simplify dependencies)

```text
Simplify (UI/mental model) ────────────────────────────►
        │
        ├── Phase A  finding/score gate before open
        ├── Phase B  one PR per finding cluster
        ├── Phase C  before/after validation bar
        ├── Phase D  PR body = why this helps
        ├── Phase E  learn from merge/reject
        └── Phase F  fleet (pinky) quality parity
```

**What simplify unlocks:** fewer PR-creating entry points (Scan-only already shipped for Onboard Results); less temptation to reintroduce Commit / Per-Agent; clearer Ledger “needs you” as the only human work. Phases A–E should attach to `auto_delivery` → `route_and_deliver` → `create_source_patch_pr` / `commit_to_infra_repo`, not new UI products.

### Phase A — Only open PRs tied to open findings / score delta

**Goal:** No PR unless the batch maps to open finding keys or a claimed score lift.

**Build on:**
- `assessment_diff.current_finding_keys()` / `diff_assessments()`
- `route_and_deliver(..., target_findings=)` already persisted on deliveries
- `auto_delivery._assessment_has_finding_category()` (extend from fix-loop to *open* gate)
- Ledger findings on Assessment Detail

**Rules:**
- Empty finding set + no material score regression → do not open PR (content-unchanged style short-circuit; extend `_infra_repo_content_unchanged` / source-patch analogue).
- Drop generated files whose category/skill is not in the open finding set (stronger than post-hoc filter of fleet junk).
- Self-managed: prefer `skills/**` that address dogfood findings over speculative chart templates.

**Simplify dependency:** Weak — can ship under Scan-only. Stronger UX copy (“no open findings → no PR”) after simplify finishes messaging.

**Done when:** Dogfood Scan with no new findings opens zero PRs; Scan with N open findings opens PRs only for those categories.

### Phase B — One PR per finding cluster (not 16 unrelated templates)

**Goal:** Partition deliverables by finding cluster (category + shared dependency), open N small PRs or one PR with N clearly separated commits — prefer **separate PRs** for independent clusters so merge/reject teaches cleanly (Phase E).

**Build on:**
- Orchestrator per-file category / “why”
- `preview_delivery_groups()` grouping instincts (reuse concepts, not Per-Agent UI)
- RemediationDispatcher path-merge discipline (exact path, not whole domain wipe)

**Rules:**
- Cluster = same finding category (or explicit dependency graph later).
- Cap files per PR (founder-tunable; start low for self-managed chart).
- Cross-cutting `skills/**` catalog improvements may be their own cluster.

**Simplify dependency:** Medium — remove Per-Agent product so this does not resurrect as a competing CTA. Implementation stays in `auto_delivery` / delivery router.

**Done when:** A multi-finding Scan produces ≤ one PR per cluster; reviewers can reject one cluster without discarding another.

### Phase C — Before/after validation (SSA + properties + no collisions)

**Goal:** Fail closed into `needs_attention` unless validation bar passes for the cluster.

**Already present (compose, don’t reinvent):**
- SSA: `dry_run_manifests_against_cluster()` (#125) — concrete YAML only
- Properties: `property_verifier.verify_all_properties()` inside `auto_delivery` loop
- Collisions: `validate_self_managed_chart_delivery()` + `_lookup_chart_path_existence`
- Filter: `filter_self_managed_delivery_files()`

**Add:**
- Per-cluster validation (don’t let a good RBAC cluster fail because an unrelated NetworkPolicy is broken — or split first via B).
- For Helm chart patches: lint/`helm template` style check where SSA cannot run; keep #119 as belt-and-suspenders.
- Optional post-merge hook path (webhook re-Assess) already feeds `correlate_delivery_finding` — ensure every Scan-opened PR records `target_findings`.

**Simplify dependency:** Low for machinery; medium for surfacing `needs_attention` only on Ledger/Scan Results.

**Done when:** No PR opens if SSA fails (concrete), property check fails for a targeted finding, or chart collision/forbidden kind; Helm skips SSA honestly with alternate check.

### Phase D — PR body = why this helps

**Goal:** GitHub PR is the review surface: finding → change → expected score/risk.

**Build on:**
- File `description` / orchestrator “why”
- Ledger finding text + severity
- Score dimension from assessment
- Filtered drop reasons (already on outcomes) — list what was *not* included and why

**Shape (minimum):**
1. Targeted findings (keys + severity)
2. Expected effect (clear finding / +score in dimension)
3. Validation summary (SSA / properties / gate)
4. File list with one-line why each
5. Explicit “Argo deploys after merge; AgentIT does not auto-merge”

Replace the thin bodies in `create_source_patch_pr` / `commit_to_infra_repo` for Scan-originated PRs.

**Simplify dependency:** Low.

**Done when:** A reviewer can approve/reject from the PR description alone without opening the portal.

### Phase E — Learn from merge/reject (don’t approve skills on open)

**Goal:** Skill reputation tracks human acceptance and post-merge outcomes.

**Build on:**
- `record_skill_outcomes()` / `SkillStore.record_skill_outcome`
- Self-managed already: `record_skill_approval=False` on open
- `agent_feedback` / rejection counts (`store/feedback.py`)
- `correlate_delivery_finding()` → `resolved` / `still_present`
- capability-scout preference for previously rejected categories

**Rules:**
- **Never** `approved` on PR open (extend fleet to match self-managed).
- On GitHub merge (existing PR tracking / webhook): provisional `merged`; on re-Assess resolve → `approved` or `ineffective`.
- On close/reject: `rejected` with finding_category — suppress or deprioritize that skill for that app/cluster.
- Do not treat LLM `review_final_manifests()` approval as human acceptance.

**Simplify dependency:** Medium — Ledger as single place to see merge/reject learning; avoid new “gates” product.

**Done when:** Skills that open junk stop being preferred; skills that clear findings gain weight.

### Phase F — Fleet-app quality (pinky path) parity

**Goal:** Same A–E bar for `apps/{app}/` infra PRs, not only AgentIT.git.

**Differences to respect:**
- Destination remains gitops + AppSet (`architecture-agentit-vs-fleet-gitops.md`)
- No Helm chart gate; instead AppSet-safe manifests + SSA dry-run (concrete YAML works here)
- Image/digest promotion stays app CI’s job

**Done when:** Pinky Scan PRs meet the shared + fleet acceptance tables; zero Application clobbers; score improves post-merge at similar merge rate to self-managed dogfood.

---

## 4. What NOT to do

| Do not | Why |
| ----- | --- |
| **Dump the skill pack** into chart/ or `apps/{app}/` | #119/#121 exist because this looked productive and was not |
| **Resurrect Per-Agent PRs as a product** | Phase B clustering belongs inside Scan/`auto_delivery`, not a second CTA (#123) |
| **Auto-merge** | Product contract: human merges on GitHub |
| **Direct Apply** | Argo is sole deployer; Direct Apply reopens dual-writer and skips review |
| **Approve skills on PR open** | Opening ≠ helping; especially wrong for self-managed (already fixed) and should die for fleet too |
| **Rewrite `argocd/application.yaml` from onboard** | Clobbers live `image.tag` / Application ownership |
| **Ship quality as a new competing workflow** | Attach to Scan → `auto_delivery` → Ledger; simplify is deleting surface area |
| **Wait for perfect LLM** | Prefer finding gates + small clusters + validation; LLM review is advisory only |

---

## 5. Success metrics

Track on dogfood (AgentIT self) first, then pinky.

| Metric | Target (founder v1) |
| ------ | ------------------- |
| **% PRs merged without force-push / emergency follow-up** | ≥ 70% of Scan-opened PRs in a 2-week window |
| **% PRs with explicit finding/score link in body** | 100% of new Scan PRs after Phase D |
| **Finding clear rate** | ≥ 60% of merged PRs → `correlate_delivery_finding` = `resolved` within one re-Assess |
| **Score improvement post-merge** | Median overall or targeted-dimension score Δ ≥ 0; no silent regressions blamed on AgentIT PRs |
| **Zero Application clobbers** | 0 onboard PRs touching `argocd/application.yaml` or forbidden kinds |
| **Zero dead-letter destinations** | 0 PRs under `apps/agentit/` in gitops |
| **Junk open rate** | PRs opened with zero mapped findings → 0 after Phase A |
| **Skill honesty** | 0 `approved` outcomes recorded at PR-open time |
| **`needs_attention` usefulness** | Human can act from Ledger reason alone (filter/gate/SSA) without reading pod logs |

---

## 6. Immediate next 2 weeks — checklist

Prioritize dogfood AgentIT.git; keep simplify unblocked.

### Week 1

- [x] **A0 — Inventory live #124-class PRs:** for each open/merged Scan PR, label: finding-linked? cluster-scoped? body useful? merged as-is?
- [x] **A1 — Finding gate spike:** in `auto_delivery` (or immediately before `route_and_deliver`), refuse open when `target_findings` empty / no score delta; land behind a clear `needs_attention` reason (Ledger-visible).
- [x] **A2 — Wire `target_findings` on every Scan delivery** (verify onboard path always passes keys from `current_finding_keys`).
- [x] **E0 — Stop fleet approve-on-open** if still default (`record_skill_approval=True`): align with self-managed honesty.
- [x] **Simplify coexistence:** no new Commit / Per-Agent / Direct Apply UI; any clustering stays server-side.

### Week 2

- [x] **B1 — Cluster partition prototype:** group files by finding category before PR open; open one PR per cluster for self-managed dogfood (cap file count).
- [x] **C1 — Compose validation bar:** SSA (#125) + property checks for targeted categories + #119 collision gate must all pass per cluster.
- [x] **D1 — PR body template v1** in `create_source_patch_pr` (and infra commit for pinky spike if cheap): findings, expected score/risk, validation summary, file whys.
- [x] **F0 — Pinky gap note:** list which of A–E already fail on fleet path (likely approve-on-open + body + clustering); do not full-parity yet unless A/B/D are stable on self-managed.
- [x] **Metrics scratchpad:** count merge-without-rewrite and finding-link % on the week’s PRs; feed Week 3 prioritization.

### Explicit non-goals for these 2 weeks

- Full skill reputation ML / new database product
- Auto-merge, Direct Apply, Per-Agent resurrection
- Replacing Argo or changing Application `agentit` ownership
- Implementing the entire Phase F pinky parity

---

## Module map (cite when implementing)

| Concern | Where |
| ------- | ----- |
| Scan pre-PR pipeline | `src/agentit/portal/auto_delivery.py` |
| Route + deliver + filter + chart gate | `src/agentit/portal/delivery.py` (`filter_self_managed_delivery_files`, `validate_self_managed_chart_delivery`, `route_and_deliver`, `correlate_delivery_finding`) |
| Self-managed HPA app-correctness | `src/agentit/portal/self_managed_hpa.py` (Rollout name/kind + RWO maxReplicas; wired into filter/gate + SkillEngine) |
| SSA dry-run | `src/agentit/portal/cluster_apply.py` (`dry_run_manifests_against_cluster`) |
| Source / infra PRs | `src/agentit/portal/github_pr.py` (`create_source_patch_pr`, `commit_to_infra_repo`) |
| Generation constraints | `src/agentit/skill_engine.py` (`SkillEngine(self_managed=…)`, `generate_for_finding`, `record_skill_outcomes`) |
| Finding keys / score diff | `src/agentit/assessment_diff.py`, `src/agentit/portal/content_diff.py` |
| Ledger / human queue | `src/agentit/ledger.py`, portal Ledger templates, `pr_tracking` |
| Normative destination rules | `docs/architecture-agentit-vs-fleet-gitops.md` |

---

## Honest sequencing note

|#124-class PRs prove the pipe works: right repo, Helm-ish, gate-passable.  
**Helpful** requires Phases A→D in order (gate on need → small scope → validate → explain). Phase E makes the system stop repeating junk. Phase F copies the bar to pinky. Simplify running in parallel should **delete competing surfaces**, not add a second quality product.
