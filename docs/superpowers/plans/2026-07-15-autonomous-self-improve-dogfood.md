# AgentIT Autonomous Self-Improvement — Dogfood Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AgentIT reliably improve *itself* end-to-end (evidence → propose → verify → PR → human merge → learn from outcome), repeatedly, so improving other apps becomes a specialization of the same loop.

**Architecture:** Two complementary loops already exist — `skill-learner` (skills catalog for *other* apps) and `capability-scout` (AgentIT's *own* repo). Today scout v1 only opens draft PRs that add `docs/proposals/<slug>.md` (no source diffs). This plan hardens the dogfood substrate, then advances scout from **doc proposals (L2)** to **gated source PRs (L3)** to **outcome-aware cycles (L4)**, with a final proof that the same engine lands a change on a non-AgentIT app (L5).

**Tech Stack:** Python 3.12, `capability_scout.py` + `watchers/capability_scout.py`, `skill_learner.py`, portal Self-Improvement routes, `git_pr.py`/`gh`, pytest CI, Argo Rollouts canary, Helm `agents.capabilityScout.*`.

**North-star definition of done (30 days):**
- ≥ 4 scout cycles that open a real PR
- ≥ 2 merged with &lt; 30 min human rewrite
- 0 merges that break portal/CI
- 0 “draft only on PVC / invisible in UI” incidents for learner or scout artifacts
- ≥ 1 merged change that moves a measured metric (test green, watcher reliability, AgentIT self-assess score, or known-gap closed)

**Non-goals (explicit):**
- Unattended merge to `main` (human merge stays through L4)
- AutoMode / silent cluster apply as part of self-improve
- capability-scout touching `chart/`, `argocd/`, secrets, RBAC (keep existing deny list)
- Customer GTM Harden-PR SKU work (parallel track; do not block this plan unless auth is required for dogfood Route)

## Global Constraints

- No mock data — proposals only from real store queries + doc-gap greps (`MIN_SIGNAL_ROWS = 5`).
- Fail closed on LLM parse errors (retry, then no-proposal — never invent).
- Diff caps remain machine-enforced: ≤3 files, ≤150 lines (`MAX_DIFF_FILES` / `MAX_DIFF_LINES`).
- Scope allowlist: `src/agentit/`, `skills/`, `checks/`, `tests/`, `docs/` only.
- Prefer fixing dogfood reliability before adding autonomy.
- Commit to `main` only via normal PR+CI; scout always uses `agentit/self-improve/*` branches.

## Levels (progress gates — do not skip)

| Level | Meaning | Exit criteria |
|-------|---------|---------------|
| **L0** | Substrate healthy | Auth or network-bound dogfood; Argo Synced; watchers steady; no mid-canary draft loss |
| **L1** | Real evidence → one grounded proposal | Scout logs evidence; LLM returns parseable `has_proposal` or honest no-op |
| **L2** | Gates → draft PR | Doc-proposal PR opens; CI runs; visible on Self-Improvement tab |
| **L3** | Human merges executable change | Source (or skill) diff PR merged with minimal rewrite; CI green |
| **L4** | Outcome feeds next cycle | Merge/close recorded; next cycle prefers open gaps / prior rejects |
| **L5** | Same loop on another app | One non-AgentIT app gets a merged improvement via the same engine shape |

---

## File map (what this plan touches)

| File / area | Responsibility |
|-------------|----------------|
| `src/agentit/watchers/skill_learner.py` | Mid-canary 404 retry; delay first tick after start |
| `src/agentit/watchers/capability_scout.py` | Startup delay; cycle telemetry; later outcome hooks |
| `src/agentit/capability_scout.py` | Evidence, `build_diff`, gates; later source-diff builder |
| `src/agentit/llm.py` | Robust JSON parse + retry for `propose_capability_improvement` |
| `src/agentit/portal/routes/capabilities.py` + Self-Improvement templates | Loop cockpit: PR status, gate table, merge outcome |
| `argocd/application.yaml` | `ignoreDifferences` for Pipeline/EventListener; scout params |
| `docs/proposals/` | L2 artifacts (existing) |
| `tests/test_capability_scout*.py`, `tests/test_skill_learner.py` | Regression for retry, JSON, source-diff gates |
| `docs/self-improvement-for-agentit.md` | Retag v1 vs v2 (source diff) when L3 ships |

---

## Phase 0 — Dogfood substrate (L0) — Week 1

**Why first:** Live evidence already showed scout/learner racing the canary (404 HTML), Argo OutOfSync on CI resources, and LLM JSON soft-fail. Autonomy on a broken substrate is theater.

### Task 0.1: Mid-rollout webhook retry (skill-learner)

**Files:** `src/agentit/watchers/skill_learner.py`, `tests/test_skill_learner.py`

- [ ] Confirm local retry loop (`_draft_retry_attempts` / 404 retry) is complete and tested
- [ ] Add startup grace: do not run `research_once()` until portal `/api/webhook/skill-draft` returns non-404 for an authed probe (or sleep N×20s after process start)
- [ ] Commit + ship; verify on next canary: learner logs show retry-then-200 **or** delayed first tick with 0 PVC-only drafts

### Task 0.2: Same class of race for capability-scout

**Files:** `src/agentit/watchers/capability_scout.py`, chart env if needed

- [ ] Delay first `research_once()` until rollout stable **or** until `gh`/git/`pytest` prerequisites + portal health are green
- [ ] On LLM/JSON failure, log structured `capability-run` with `outcome=parse-error` (not silent “no proposal” that looks like “no evidence”)
- [ ] Ship; confirm next scout tick after deploy does not coincide with canary skew

### Task 0.3: Argo self-heal credibility

**Files:** `argocd/application.yaml` (uncommitted ignoreDifferences already drafted)

- [ ] Land Pipeline/EventListener `ignoreDifferences` (webhook-normalized fields)
- [ ] Sync; confirm Application **Synced + Healthy** for a boring 48h
- [ ] Add a portal Health assertion or synthetic check that fails dogfood if AgentIT itself is OutOfSync

### Task 0.4: Dogfood access control

**Files:** `argocd/application.yaml`, `docs/deployment.md`

- [ ] Enable `auth.enabled=true` on the dogfood Route (oauth-proxy)
- [ ] Verify Self-Improvement + Capabilities still work as an authenticated user
- [ ] Document “dogfood cluster must have auth on” as a hard rule in CLAUDE.md / README Security notes

### Task 0.5: Freeze a blessed dogfood tag

- [ ] Stop piloting from dirty `main`; cut `dogfood/self-improve-YYYYMMDD` tag (or release branch) after Phase 0 merges
- [ ] Argo tracks that revision for the milestone window

**Phase 0 exit:** Synced Argo, auth on, one canary deploy with **zero** PVC-only learner drafts, scout first-tick no false 404/race.

---

## Phase 1 — Honest L1/L2 (proposal quality) — Week 1–2

**Today:** Scout can gather evidence and open doc PRs, but LLM JSON already failed once live (`LLM returned unparseable capability proposal`).

### Task 1.1: Harden `propose_capability_improvement` parsing

**Files:** `src/agentit/llm.py`, `tests/test_llm.py`

- [ ] Strip markdown fences; retry once with “return raw JSON only”
- [ ] Validate required keys (`has_proposal`, `title`, `evidence`, `test_plan`, …)
- [ ] On failure → `has_proposal: false` path with logged parse-error (fail closed, no invent)

### Task 1.2: Evidence quality bar

**Files:** `src/agentit/capability_scout.py`

- [ ] Weight doc-gap hits first (already intended); add explicit ranking in prompt payload
- [ ] Include AgentIT’s own assess score / recent tick failures / low-effectiveness skills in evidence blob (already partially there — verify live `gather_evidence` output in a `capability-run` details JSON)
- [ ] Add CLI `agentit propose-once` (if missing) for manual dogfood cycles without waiting 24h

### Task 1.3: Portal cockpit (make the loop visible)

**Files:** Self-Improvement routes/templates, `llm_decisions.py` if needed

- [ ] Each run shows: signal_count, top evidence, gate pass/fail table, PR URL, live PR state (`gh`/`get_pr_status`)
- [ ] Distinguish outcomes: `no-signal` | `parse-error` | `no-proposal` | `gate-blocked` | `proposed` | `pr-failed`
- [ ] Badge when maxOpenPRs blocks a cycle

### Task 1.4: Cadence for dogfood (temporarily)

**Files:** `argocd/application.yaml`

- [ ] Set `agents.capabilityScout.interval` to `3600` (1h) or `7200` for the 30-day milestone only
- [ ] Keep `maxOpenPRs: 1`
- [ ] Calendar reminder to restore 24h after milestone

**Phase 1 exit:** 3 consecutive cycles with parseable outcomes; ≥1 draft doc PR opened and visible in UI; human can merge or close with reason recorded.

---

## Phase 2 — Cross L3: executable self-improve (the real milestone) — Week 2–3

**Gap:** Module docstring in `capability_scout.py` explicitly says v1 does **not** apply source diffs. L3 requires closing that gap carefully.

### Task 2.1: Design the source-diff path (small, fail-closed)

**Files:** helpers in `src/agentit/capability_scout.py` + `LLMClient.generate_capability_files`, tests first

Allowed change classes for v1 source autonomy (pick in order):
1. **Skill/check markdown** under `skills/` / `checks/` (safest — mirrors skill-learner)
2. **Test-only fixes** under `tests/` (prove gates + pytest)
3. **Narrow bugfix** under `src/agentit/` with file contents fed into the LLM — **deferred**; v1 source allowlist is `skills/`|`checks/`|`tests/` only

- [x] Write tests for: resolve_build_mode, source allowlist, drop out-of-target LLM paths, docs fallback
- [x] Implement `build_source_diff(proposal, repo_dir, llm_client) -> dict[path, content]` that:
  - reads current file text for each `target_files` entry
  - asks LLM for full-file replacement **only for those files**
  - rejects if paths drift outside proposal allowlist
- [x] Fall back to docs proposal when LLM returns nothing / ineligible targets

### Task 2.2: Gate upgrade

**Files:** `run_safety_gates`

- [x] Existing gates still apply to source diffs (size, scope, secrets, test_plan, py_compile, pytest suite)
- [ ] Optional follow-up: fail hard if `mode=source` requested but only docs/proposals landed (today we soft-fallback to docs)
- [x] Keep secret regex scan

### Task 2.3: Dual-mode scout

**Files:** `watchers/capability_scout.py`, values `agents.capabilityScout.mode: docs|source|auto`

- [x] `docs` = today’s behavior (safe fallback / chart default)
- [x] `source` = Task 2.1 path (falls back to docs if ineligible)
- [x] `auto` = source when all targets ⊆ skills|checks|tests; else docs
- [x] Dogfood Helm: `agents.capabilityScout.mode=auto` in `argocd/application.yaml`; CLI `--mode`; chart template args

### Task 2.4: First merged source PR (manual governor)

- [ ] Commit + push Phase 2 + prometheusrule fix so Argo can clear ComparisonError and scout redeploys with `mode=auto`
- [ ] Trigger `propose-once --mode auto` (or wait for hourly tick) until a low-risk source PR opens
- [ ] Human reviews; merge if CI green
- [ ] Record time-to-merge and edit distance (lines you rewrote)
- [ ] Repeat until **2 merges** meet north-star (&lt;30 min rewrite)

**Phase 2 exit:** L3 achieved — two merged executable self-improve PRs; docs updated to say source mode is real. (Code path shipped locally; live merges still open.)

---

## Phase 3 — L4 outcome loop — Week 3–4

### Task 3.1: Persist merge/close outcomes

**Files:** store (`store.py` / `store_pg.py`), webhook or poll job

- [ ] On each open `agentit/self-improve/*` PR, poll `get_pr_status` from portal maintenance or scout tick
- [ ] Log `capability-outcome` event: merged | closed | stale
- [ ] Store reason if closed (label or PR comment convention: `agentit:reject-reason:…`)

### Task 3.2: Feed outcomes into next `gather_evidence`

- [ ] Prefer unresolved doc gaps and previously rejected categories
- [ ] Deprioritize titles/slugs closed as `wontfix` in last N days
- [ ] If last merge broke CI (detect via subsequent failed runs), force next cycle to “fix regression only” mode

### Task 3.3: Skill-learner ↔ scout contract

- [ ] skill-learner drafts stay in catalog Activate path (other-apps loop)
- [ ] scout may propose skill fixes when effectiveness is low — but one owner per artifact (no double PRs)
- [ ] Document the split in README Self-improvement sections

**Phase 3 exit:** A closed PR changes the next cycle’s proposal; a merged PR is cited as evidence in a later run’s details JSON.

---

## Phase 4 — L5 “trivial for other apps” proof — Week 4

### Task 4.1: One external app, same engine shape

- [ ] Pick one fleet app (e.g. pinky/guestbook) already GitOps-managed
- [ ] Run Assess → Generate → GitOps PR (customer wedge) **using the same delivery + gate discipline**
- [ ] Separately, if applicable, run a skill improvement that originated from learner and was activated after scout-quality Activate checks

### Task 4.2: Write the milestone retrospective

**Files:** `docs/dogfood-self-improve-milestone.md`

- [ ] Evidence: PR links, merge times, metrics moved, failures hit, what remains manual
- [ ] Explicit claim: “L4 on AgentIT; L5 sample on app X”
- [ ] Explicit non-claim: AutoMode / unattended merge still off

**Phase 4 exit:** Public (internal) write-up + demo path another engineer can replay.

---

## Parallel track (do not confuse with this plan)

| Track | Purpose |
|-------|---------|
| Harden-PR GTM (#1 dual PR) | External validation / revenue story |
| This dogfood plan | Engine proof / moat story |

Rule: **dogfood P0s (Phase 0–2) beat GTM polish** until L3 is hit. After L3, split eng capacity 50/50.

---

## Kill switches (stop and reassess)

- 10 scout cycles with 0 mergeable PRs → fix proposal/gates, don’t add more watchers
- Any merged self-improve PR breaks production portal → freeze `source` mode, revert to `docs`, postmortem
- Mid-canary draft loss recurs after Task 0.1/0.2 → block further autonomy work until fixed
- LLM spend without merges → lower interval back to 24h and tighten prompts

---

## Suggested execution order (checklist summary)

1. Phase 0.1–0.5 substrate  
2. Phase 1.1–1.4 proposal honesty + cockpit + hourly dogfood cadence  
3. Phase 2.1–2.4 source diffs + two merges  
4. Phase 3 outcome feedback  
5. Phase 4 external app proof + write-up  

---

## Immediate next action (start tomorrow)

Ship **Task 0.1 + 0.2 + 0.3** from the already-dirty working tree (learner retry, scout startup grace, Argo ignoreDifferences), cut a dogfood tag, then run `agentit propose-once` (or force a scout tick) and record whether you get a parseable L1/L2 outcome on the live cluster.
