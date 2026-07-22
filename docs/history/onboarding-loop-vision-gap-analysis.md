# The product owner's "one simple loop" vision — a grounded gap analysis

> **HISTORICAL planning record (2026-07-22).** Written when Direct Apply,
> Per-Agent PRs, and AutoMode were still in play. The live product is
> **Scan-only HITL → GitOps PR → human merge → Argo**. Use
> [../README.md](../README.md) and [architecture.md](./architecture.md) for
> current truth. Keep this file for the gap analysis / phase history only.

**Status: mostly SHIPPED (as of 2026-07-17 closing section), then further
superseded by skills-primary + Scan-only + quality PRs (2026-07-21).** See
"Final status" at the end. Body below is preserved as planning record.

**Original status: design/planning analysis, no code changes.** Written
against `AgentIT-ui-redesign` @ `origin/main` (pulled fresh this session —
HEAD `0e66fb7`). This is not a third, competing proposal — it evaluates the
product owner's own 8-step brainstorm against what's actually built today,
and against the two design documents already produced earlier in this same
work cycle:

1. `docs/unified-apply-flow.md` — the shipped `route_and_deliver()` router
   and its GitOps-vs-direct-apply decision logic (**Status: implemented**
   per its own header).
2. **"AgentIT: Consolidating Delivery Paths + Adding an LLM Manifest-Review
   Gate"** — a design proposal produced earlier today in a prior session of
   this same repo, delivered directly in that session's conversation rather
   than committed as a file (it is not present anywhere under `docs/`; this
   doc is the first place its content is written to disk). Referred to
   below as **"the consolidation proposal."** Its three headline moves:
   fold "Per-Agent PRs" into the one Deliver action as a checkbox, make
   GitOps the default path with Direct Apply as an explicit, friction-full
   opt-out, and add a mandatory LLM manifest-review gate inside
   `route_and_deliver()`.

Every claim below was re-verified directly against the working tree during
this pass, not copied from either source doc — where a source doc's
description turned out stale, incomplete, or (in one case, see §1) actually
contradicted by the current code, that is called out explicitly rather than
silently repeated.

## Executive summary

The product owner's vision is **not a new architecture** — it is "finish
what the last two docs already started, and go one step further on
automation." Every one of the 8 steps maps onto infrastructure that
already exists in some form:

- Steps 1 and 3 (mandatory GitOps, automatic PRs) are the consolidation
  proposal's own Part 2/Part 3, just with "explicit opt-out" tightened to
  "no opt-out at all" — except this pass found a **real, previously-unnamed
  bootstrap bug** that blocks literally removing Direct Apply (§1).
- Step 2 (no difference between assess and onboard) already has a partial,
  shipped implementation (`continue_onboard`/"Refresh Onboard") that the
  vision wants extended from "re-assess only" to "every assess" — which
  collides with a real, deliberate human-review checkpoint (§2).
- Step 4 (notification) and step 6 (merge/rollout detection) are the same
  underlying gap: AgentIT has rich **in-app** signal (Ledger, Fleet's
  "needs action" badge, gates) and zero **outbound** signal (no
  Slack/PagerDuty/email, no webhook on the infra repo, `get_pr_status()`
  only ever polls on page load) — `docs/unified-apply-flow.md:397` already
  names half of this gap explicitly.
- Step 5 (UI-merge vs. GitHub-merge) has a real, code-grounded answer
  below, not just a menu of options.
- Step 7 (verify the fix worked) is the one step with a genuine
  "dead code" story: `property_verifier.py::verify_all_properties()` is
  fully implemented and reachable only via one unwired JSON API endpoint
  (`GET /api/assessments/{id}/verify`, `assessments.py:1234-1266`) — but it
  answers a narrower question than the vision needs (see §7 for exactly
  what it does and doesn't check).
- Step 8 (the loop repeats) already half-exists for the *re-assessment*
  case (`webhooks.py::webhook_github_push`, lines 251-259) but not for a
  human-initiated re-Assess.

Nothing here requires inventing a new subsystem. It requires: closing one
real bootstrap gap, deciding how far to push automation past today's human
checkpoints, and wiring three pieces of already-built machinery
(`verify_all_properties`, `drift_detector`'s Argo poll, `get_pr_status`)
into a UI flow that doesn't yet call them together.

---

## 1. "Setup Argo is mandatory, no more direct updates to cluster"

### What exists today

- **"AgentIT auto-creates a GitOps repo if the user leaves it blank"
  already exists and already runs by default**, not just as an
  opt-in nudge: `assess_submit()`'s background job
  (`assessments.py:82-96`, `_assess_sync`/`_auto_create_infra_repo`) calls
  `_auto_create_infra_repo()` for *every* assessment that doesn't supply an
  `infra_repo_url`, which calls `github_pr.ensure_infra_repo()`
  (`github_pr.py:812-890`) — creates a private `agentit-gitops` repo under
  the app owner's account (or reuses one that already exists) and seeds an
  empty `apps/.gitkeep`. The Fleet "New Assessment" modal
  (`fleet.html:176-178`) already tells the user this explicitly: *"Leave
  blank and AgentIT will scaffold one for you."* So step 1's "agentit auto
  creates one if user leaves it blank" is **already true today**, not a
  gap — it happens on literally every assessment, registered or not.
- **A distinct, lighter-weight "Register for GitOps" action also exists**
  for an already-assessed app that didn't get an infra repo the first time
  (`register_gitops`, `assessments.py:398-453`): sets
  `report.infra_repo_url`, auto-creates the repo if none supplied, and
  calls `ensure_applicationset()` (`github_pr.py:739-809`) to create/patch
  the live Argo CD `ApplicationSet` (`selfHeal: true`, `prune: true`,
  watching `apps/*`).
- **Direct Apply is not currently the default when unregistered — it's the
  *only reachable outcome* when unregistered**, and the router already
  actively prevents the double-write footgun `docs/unified-apply-flow.md`
  was written to close: `route_and_deliver()` (`delivery.py:381-627`) checks
  `is_gitops_registered()` (`delivery.py:174-198`) before ever calling
  `apply_manifests_to_cluster`, so a registered app's cluster-config files
  never get both a direct apply *and* a GitOps commit for the same delivery.

### What's genuinely missing — and one real bug this pass found

**There is a live, unfixed bootstrap circularity that literally blocks
removing Direct Apply.** Walk the exact sequence a brand-new app follows
today:

1. Assess → `_auto_create_infra_repo` (or a human-supplied repo) sets
   `infra_repo_url`. `ensure_applicationset()` creates the `ApplicationSet`
   CR in the cluster. **Nothing has been committed to the infra repo's
   `apps/` directory yet.**
2. Onboard → generates manifests.
3. Deliver → `route_and_deliver()` calls `is_gitops_registered(app_name,
   report)`. This does a **live** query for an Argo CD `Application` named
   `managed-{app}` (`delivery.py:174-198`, `gitops_application_name()` at
   `delivery.py:160-171`). That `Application` is only generated by Argo's
   `ApplicationSet` controller once a matching `apps/{app}/` directory
   *actually exists* in the infra repo's git tree (the git generator config
   in `ensure_applicationset`, `github_pr.py:761-771`, watches `apps/*` as
   real directories, not a promise). **No directory exists yet — nothing
   has committed one.** So the live query correctly finds nothing, and
   `is_gitops_registered()` returns `(False, infra_repo_url)`.
4. Back in `route_and_deliver()` (`delivery.py:473-480`):
   ```python
   if registered and infra_repo_url is None:
       mechanisms[CATEGORY_CLUSTER_CONFIG] = MECHANISM_NONE
   else:
       mechanisms[CATEGORY_CLUSTER_CONFIG] = (
           MECHANISM_INFRA_REPO_COMMIT if registered else MECHANISM_DIRECT_APPLY
       )
   ```
   `registered` is `False` here (not the `MECHANISM_NONE` case — that guards
   the *opposite* mismatch: a live `Application` found, but *this*
   report's `infra_repo_url` unknown). So this resolves to
   **`MECHANISM_DIRECT_APPLY`**, every time, for every brand-new app,
   registered or not.
5. Direct Apply writes straight to the cluster namespace. It **never
   commits anything to the infra repo.** So `apps/{app}/` never gets
   created in the infra repo's git tree. So the `ApplicationSet` never
   discovers it. So `is_gitops_registered()` **can never return `True` for
   this app via this flow, ever** — it's a closed loop, not a one-time
   bootstrap step.

Nothing in the codebase currently seeds the infra repo's `apps/{app}/`
directory except `commit_to_infra_repo()` (`github_pr.py:608-731`), and the
only caller of that function is `deliver_with_verification(mechanism=
MECHANISM_INFRA_REPO_COMMIT, ...)`, only reachable when `registered` is
already `True`. **This is a genuine, previously-unnamed gap**: today, in
production, against a real cluster, "Register for GitOps" plus a real
Deliver click does not actually get a new app under GitOps management —
it silently keeps direct-applying forever, while `register_gitops`'s own
success message (`assessments.py:447-452`) correctly warns *"This app will
show as GitOps-registered once your next Fix/Onboard delivery is committed
and merged there"* — but nothing in the code ever performs that first
commit. The consolidation proposal's §1.6 states this edge case "refuses to
guess, produces `MECHANISM_NONE`" — that description does not match the
current code path for this exact scenario (it matches only the rarer
mismatched-report case); this pass corrects that record.

**Consequence for "mandatory, no more direct apply":** removing Direct
Apply as a mechanism entirely, without first fixing this, would leave
every newly-onboarded app with **no way to ever reach a delivered state at
all** — `route_and_deliver()` would compute `MECHANISM_DIRECT_APPLY` for
the cluster-config category and then have nowhere to send it. This is the
literal "bootstrap-before-an-infra-repo-exists" case the task asked about,
and it is not hypothetical — it is the *only* path every app takes today.

### What would need to change

1. **Fix the bootstrap gap first, independent of anything else in this
   doc.** The cleanest fix that reuses existing code: when `route_and_
   deliver()` finds `infra_repo_url is not None` but `registered is
   False`, treat that as "first-ever commit," not "not registered" —
   route to `MECHANISM_INFRA_REPO_COMMIT` unconditionally the first time an
   infra repo URL is known, exactly the same commit-and-PR call
   (`commit_to_infra_repo`) already used for the registered case. Once that
   first PR merges and Argo's `ApplicationSet` discovers `apps/{app}/`, all
   *subsequent* deliveries correctly see `registered=True` via the live
   query. This turns "registered" from a strict precondition into what it
   should be: a signal used only to confirm the delivery already
   succeeded, never a gate on whether to attempt it. `DriftDetector.
   _maybe_close_gitops_delivery()` (`drift_detector.py:148-185`) already
   has the matching half of this — it already reconciles a pending
   delivery's commit SHA against Argo's synced revision without requiring
   `registered` to have been `True` at commit time — the write side (the
   fix above) just needs to reach parity with the read side that already
   exists.
2. **Then, and only then, does "mandatory" become a real option**: with
   the bootstrap fixed, remove `MECHANISM_DIRECT_APPLY` from
   `route_and_deliver()`'s cluster-config branch entirely (not just
   de-prioritize it per the consolidation proposal's opt-out framing) —
   every cluster-config delivery becomes infra-repo-commit, full stop, with
   no `report.gitops_opt_out` escape hatch at all. This is a strictly
   larger removal than the consolidation proposal's Part 2 §3, which
   deliberately keeps a persisted opt-out field and a "first apply before
   an infra repo exists" allowance — the product owner's "no more" is
   explicit that neither should exist.
3. **`AutoMode`'s and every gate type's direct-apply branches evaporate
   as live code paths**, not just become less likely: `AutoMode._finish_
   direct_apply()` (`automode.py:455-547`), `resolve_gate()`'s
   `cluster-conflict-review`/`cluster-admin-review` force-apply branches
   (`gates.py:198-291`, which call `apply_with_verification`/
   `apply_manifests_to_cluster` directly, bypassing the router entirely by
   design for those two gate types) all still call `apply_manifests_to_
   cluster` under the hood via `kube.apply_yaml()` — these are the CI/CD
   shared-namespace and force-reapply-after-conflict cases, which are
   **not** the same category as "cluster/app config" and were never meant
   to be GitOps-routed (the consolidation proposal's own taxonomy in
   `docs/unified-apply-flow.md`'s §D explicitly separates them). "No more
   direct updates to cluster" needs an explicit decision on whether these
   two stay as real, rare, human-gated exceptions (recommended — they are
   fundamentally different from "should this fix be delivered," they are
   "does this service account have RBAC for a shared namespace") or whether
   the product owner's "no more" is meant to reach them too.
4. **`cli.py`'s `agentit self-fix --create-pr`** (`cli.py:679-908`) never
   touches a cluster at all — it writes to local disk and pushes a branch.
   It's outside "no more direct updates to cluster" by construction, not
   by fix.

---

## 2. "No difference between assessment and onboarding"

### What exists today

- **A real, shipped, partial version of exactly this already exists**,
  scoped to re-assessment only: the `continue_onboard` flag
  (`assess_submit()`, `assessments.py:105-122`) makes `assess_progress()`
  (`assessments.py:195-222`) auto-create and background-launch an
  onboarding job the instant the assessment job completes
  (`s.claim_continue_onboard`, atomically guarded against a double-fire
  from the htmx poll). This is Fleet's **"Refresh Onboard"** button
  (`fleet.html:103-121`) and the command palette's equivalent
  (`base.html:1686-1688, 2153-2165`) — both post `continue_onboard=1`.
  `assess_progress.html:37-38` already tells the user *"After scoring,
  onboarding will start automatically."*
- **This is deliberately gated to `r.ever_onboarded` apps only**
  (`fleet.html:106`, `assessment_detail.html:119-125`). A never-before-
  onboarded app's Assess flow ends on Assessment Detail with an explicit
  manual **"Onboard This App"** call to action
  (`assessment_detail.html:122-124`) — the code comment there is explicit
  about why: *"Onboard generates remediations for all findings — do not
  fix them one-by-one on the Actions tab first."*
- `assess_submit`/`onboard_submit` are genuinely two different routes with
  two different background-job mechanisms (`create_assessment_job` +
  `threading.Thread`-based bridge vs. `create_remediation_job` +
  `BackgroundTasks`), not just two labels on the same code path — merging
  them is a real refactor, not a UI relabel.

### What's genuinely missing

The gate that exists (`ever_onboarded`) is a **history-based** heuristic
("you've reviewed this app's findings before, so skip the review step this
time"), not a **first-time human-review checkpoint** that the vision wants
removed entirely. There is no mechanism today that lets a brand-new app
skip straight from Assess to a generated, PR-ready fix set without a human
looking at Assessment Detail's findings first — that's not a missing
feature, it's the app's one deliberate design decision working as intended.

### What would need to change

1. **Extend `continue_onboard` to fire unconditionally**, not just for
   `ever_onboarded` apps — the plumbing (`assess_submit`'s flag,
   `assess_progress`'s atomic claim-and-launch) already generalizes
   cleanly; the only change is who's allowed to set the flag (currently:
   only the two chained-UI call sites). This is a small, mechanical change
   *if* the product decision below is made.
2. **The real decision this forces**: `assessment_detail.html`'s own
   comment names the reason findings-review currently happens first —
   "do not fix them one-by-one... Onboard generates remediations for all
   findings." Collapsing assess→onboard removes the one point where a
   human sees the *raw findings* (severity, category, description) before
   any fix is generated for them. This is a real, named tradeoff, not a
   free consolidation: today, a human can look at Assessment Detail, see
   "12 findings, 3 critical," and decide *not* to onboard (e.g., the repo
   was a mistake, or findings need triage first). Full automation removes
   that decision point — fixes get generated and PR'd before any human
   opinion is possible. This is consistent with (not contradicted by) the
   product owner's step 3 ("no difference between assessment and
   onboarding... PRs are automatically created") — but it should be a
   named, explicit product decision, not an implicit side effect of
   flipping one flag.
3. **`RemediationLoop.trigger()`** (`remediation_loop.py:209-300`) already
   *is* this exact assess→onboard→auto-apply→verify chain for the fully
   autonomous, watcher-triggered case (webhook-driven, not portal-UI-
   driven) — it calls `/api/webhook/assess` then `/api/webhook/onboard`
   then `/api/webhook/auto-apply` in sequence with no human step between
   any of them. The vision's step 2/3 is best understood as: **make the
   portal UI's human-initiated path behave the way `RemediationLoop`'s
   webhook-initiated path already does**, not as inventing new
   orchestration.

---

## 3. "PRs are automatically created against the code repo or GitOps repo"

### What exists today

- The full mechanism inventory from both source docs holds today,
  re-verified: `route_and_deliver()` already has every mechanism this step
  needs (`MECHANISM_INFRA_REPO_COMMIT`, `MECHANISM_SOURCE_REPO_PR`,
  `MECHANISM_APP_REPO_PR`) wired to real `github_pr.py` functions
  (`commit_to_infra_repo`, `create_source_patch_pr`, `create_onboarding_pr`).
- **A human click is required for every one of these today, for every
  app that isn't allowlisted for auto-mode.** `deliver()`
  (`assessments.py:985-1086`) is a `POST` route, fired only by a human
  clicking "Apply to Cluster"/"Commit & Open PR" on Onboard Results. There
  is no code path that calls `route_and_deliver()` without either an
  explicit human `POST`, a resolved gate, or `AutoMode.execute()` — and
  `AutoMode.execute()` itself only fires when `auto_mode` is on **and**
  (per `should_auto_apply()`, `automode.py:261-298`) the orchestrator's own
  `auto_approve` flag says so **and** the LLM classifies the change as safe.
- **"Per-Agent PRs" is confirmed to still exist as a fully separate,
  ungated mechanism**, exactly as both source docs found:
  `create_agent_prs_route` (`assessments.py:1178-1231`) →
  `github_pr.create_agent_prs()` (`github_pr.py:308-456`) commits every
  generated file, grouped by agent category, straight to the app's own
  repo. It does not call `classify_file()`, never checks GitOps
  registration, and has no secret-block or placeholder-strip guard — the
  one delivery-shaped mechanism in the whole codebase that bypasses every
  safety check `route_and_deliver()` enforces for everyone else. This is
  unchanged since both source docs were written.
- **Even the GitOps commit path, once delivered, still requires a second
  human click** beyond the first Deliver click: `route_and_deliver()`
  itself now creates a `gitops-pr-pending` gate on a successful infra-repo
  commit (`delivery.py:531-549`, comment: *"Mirror AutoMode: portal Deliver
  opens the PR; Approve & Deliver on gitops-pr-pending merges it"*) — this
  is new since the consolidation proposal's session (it names this as a
  gap in §1.6: *"the manual Assess→Generate→Deliver path had no Gate step
  for GitOps apps"* — that gap is now closed for the manual path too, not
  just AutoMode).

### What's genuinely missing

**Full automation for every onboarding — not just allowlisted auto-mode
apps — does not exist, and the vision is explicitly asking for the
allowlist model to become the default, not an opt-in.** Today:

- `AutoMode`'s allowlist (`auto_mode_allowlist` setting,
  `automode.py:28, 42-58, 92-155`) is an **additive, opt-in scoping layer**
  on top of a **global on/off `auto_mode` setting** — with no allowlist
  configured, `auto_mode` either applies to every eligible manifest or
  none. There is no per-app "this app is fully autonomous" setting
  independent of the global toggle.
- The LLM safety classification (`should_auto_apply()`) gates *whether*
  auto-apply is allowed for a given batch, but nothing gates *whether a
  human is required to click Deliver in the first place* for a
  non-auto-mode app — that's simply always true today.

### Is this "auto-mode should be the default for every app"? Yes — and the real safety implications

Confirmed: the vision's steps 3-4 are functionally "treat every onboarding
the way `AutoMode.execute()` already treats an allowlisted app" — generate,
classify, deliver, gate-for-merge, notify — with the human's only
remaining click being the merge, not the "start delivery" decision. The
real safety implications, grounded in what already exists:

1. **The LLM classification gate (`classify_action`, used by
   `should_auto_apply()`) is a *destructiveness* classifier, not a
   *correctness* classifier.** It answers "is this action risky," not
   "does this manifest actually fix the finding it claims to." Making
   auto-mode the default multiplies the number of manifests that reach a
   cluster or an open PR having passed only that one check — which is
   exactly the gap the consolidation proposal's Part 3 (the LLM
   manifest-review gate) was designed to close. **Making step 3 safe at
   scale is not optional once step 3 becomes the default — it is the
   consolidation proposal's Part 3, now load-bearing rather than a nice-to-
   have**, because the human-reviews-before-delivery checkpoint this
   removes was the *other* thing catching a wrong-but-not-destructive fix.
2. **The allowlist model's one hard safety property survives intact**
   regardless of default-vs-opt-in: `RBAC_SHAPED_KINDS`
   (`automode.py:39`, `Secret`/`Role`/`RoleBinding`/`ClusterRole`/
   `ClusterRoleBinding`) are permanently non-allowlistable, and
   `CATEGORY_SECRET_BLOCKED` (`delivery.py:35, 141-146`) is a permanent,
   non-overridable deny-rule inside the router itself, not the allowlist —
   these hold no matter how automation-by-default is implemented, since
   they live below the allowlist layer, not inside it.
3. **A human still merges every GitOps PR, even under full auto-mode**,
   per `AutoMode._finish_gitops_pr()`'s explicit design
   (`automode.py:415-453`, and `docs/unified-apply-flow.md` section (B)'s
   stated rationale: merging into a `selfHeal`+`prune` repo is a bigger
   blast-radius grant than a direct apply, independent of how much the LLM
   is trusted). This is the one checkpoint the vision's step 5 asks about
   directly — see below.

### What would need to change

1. Add a per-app (not just global) "fully autonomous onboarding" flag —
   or, simpler, make `auto_mode` + a wildcard allowlist entry (`"*/*"`
   minus the permanent RBAC exclusions, which `_pattern_allows` already
   supports today with zero code change) the *recommended default
   configuration* rather than a manually-discovered opt-in. This is a
   settings/UI change, not a new mechanism.
2. Wire onboarding's own completion (`_run_onboarding_job`,
   `assessments.py:583-669`) to call `AutoMode.execute()` (or the
   consolidation-proposal-shaped equivalent) automatically once files are
   generated, instead of stopping at "manifests saved, go look at Onboard
   Results" — this is the concrete code change behind step 3's "PRs are
   automatically created."
3. Ship the consolidation proposal's Part 3 (LLM manifest-review gate)
   *before or alongside* this, not after — per that proposal's own §"Cost/
   latency" reasoning, it should be **mandatory or automatic on the
   auto-mode path specifically** (not opt-in the way it's recommended for
   the human-driven path), which is exactly what "auto-mode is now the
   default" requires.
4. Fold "Per-Agent PRs" into the router per the consolidation proposal's
   Part 2 §1 regardless of the above — it is the one mechanism today with
   zero of the safety checks the rest of this section depends on, and
   leaving it as an independent, ungated button while making everything
   else auto-mode-by-default would make it the *least* safe path in the
   app, not a parity option.

---

## 4. Notification that PRs need attention

### What exists today

- **Fleet's "needs action" badge**: `_attach_pending_actions()`
  (`fleet.py:118-151`) computes a per-app count of pending, app-owner-scoped
  gates (excluding `cluster-admin-review`, which gets its own cross-app
  count) via a single `GROUP BY repo_url` query — a real, live, in-app
  signal.
- **Ledger's "Needs You" default view**: `/` now redirects straight to
  `/ledger` (`fleet.py:153-156`, comment: *"Ops home is the Ledger (Needs
  You inbox)"*), and `recent_watcher_failures()` (`ledger.py:161-172`)
  surfaces a fleet-wide banner for any watcher whose last tick failed in
  the configured window. Every gate, delivery, and decision the app makes
  becomes a card here (`get_ledger_cards()`, `ledger.py:285-334`), unioning
  `events`/`gates`/`deliveries`/`skill_effectiveness` — this is real,
  substantial in-app visibility, not a stub.
- **This is confirmed, in `docs/unified-apply-flow.md:394-401` itself, to
  stop at the app's own boundary**: *"AgentIT has no visibility into
  [a PR merge] at all beyond the next time someone loads
  `onboard_results.html`... there is no push notification, no webhook on
  the infra repo."* Re-verified this session: still true. `webhooks.py`
  registers exactly one inbound webhook type (`X-GitHub-Event: push`,
  `webhook_github_push`, `webhooks.py:153-282`), and only on the **app's
  own** repo (`ensure_webhook` call in `_run_onboarding_job`,
  `assessments.py:642-651`) — never on the infra repo, and never for a
  `pull_request` event (merged/closed) on either repo.

### What's missing

- **No outbound notification channel exists anywhere in the codebase.**
  A repo-wide check for Slack/PagerDuty/email/webhook-out integration
  turned up nothing beyond `events.py`'s `TOPIC_ALERTS` Kafka topic
  constant — which is published-to by `slo_tracker.py`/`vuln_watcher.py`
  for internal event-bus consumption, not delivered anywhere a human would
  see it without already being logged into the portal.
- This session found **no dedicated "add alerting integration" roadmap
  item in either source doc** — `docs/proposals/add-failure-alerting-
  retry-logic-for-the-remediation-loop-ag.md` exists but is scoped
  narrowly to the `remediation-loop` agent's own success-rate monitoring,
  not general delivery/PR notifications; it is not the same gap. The
  specific, correctly-grounded citation for "no push notification" is
  `docs/unified-apply-flow.md:397`, quoted above.

### Is in-app Ledger/Events sufficient, or does this vision need real push notifications?

**In-app is sufficient for "AgentIT recorded that this needs attention."
It is not sufficient for the product owner's literal step 4 ("user is
notified")** — a notification, by definition, has to reach the user
without them first opening the portal. Today, "notified" only happens if
the user is already looking at Fleet, Ledger, or Assessment Detail's
Actions tab. For a vision that explicitly wants a human to be pulled in
only at merge/review time (steps 4-5) rather than sitting in the portal
watching Onboard Results, that gap matters more here than it would for a
human who's already mid-workflow in the app.

### What would need to change

A minimal, additive integration point, not a redesign: `EventPublisher.
publish()` (`events.py`) already has one call site per notable action
(gate created, delivery outcome, PR opened) — the natural place to add an
outbound webhook/Slack-message dispatch is a new subscriber on the
existing Kafka topics (`TOPIC_GATES`, `TOPIC_ALERTS`), not a change to any
of the publish-call sites themselves. This is genuinely new work (no
partial implementation exists to wire up), unlike most of the rest of this
document.

---

## 5. UI-merge vs. GitHub-merge — recommendation

### What exists today

- **`github_pr.merge_pr()` already exists and is already wired into a real
  UI action**: `gates.py`'s `gitops-pr-pending` resolution branch
  (`gates.py:161-196`) calls it directly — a human clicking "Approve" on
  that gate card (rendered via the shared `_macros.html::gate_card()`
  macro, used identically on Assessment Detail's Actions tab, Fleet, and
  the retired-but-redirecting Gates page) *is* the merge action. This is
  real, shipped, in-production UI-merge — not a prototype.
- **No real diff-rendering exists anywhere in the portal for a PR's actual
  GitHub diff.** `content_diff.py::diff_lines()` exists and is used
  (`onboard_results()`, `assessments.py:855-864`) — but it diffs a human's
  *own edit* against AgentIT's original generation, entirely client-side,
  before any PR exists. Nothing renders GitHub's own diff view, CI check
  status, or review-thread state inline. `get_pr_status()`
  (`github_pr.py:38-105`) fetches `state`/`merged_at`/`title`/`body`/
  `labels`/`created_at` — explicitly not files-changed or a diff.
- Gate cards' one review affordance is a **"Preview Files" link**
  (per the consolidation proposal's §1.3 inventory, confirmed against
  `gate_card()`'s actual rendering) — not an inline diff, not CI status,
  not GitHub's own review UI.

### The tradeoffs, concretely

| | UI-merge (current `gitops-pr-pending` pattern, extended) | GitHub-merge (send the human to GitHub) |
|---|---|---|
| **What's already built** | `merge_pr()`, the gate queue, gate cards, audit logging on gate resolution | Nothing extra — a link is a link |
| **Diff/review quality** | Whatever the portal renders inline (today: nothing beyond a file list) | GitHub's actual diff view, inline comments, required-check status, review requests — all real, all free |
| **CI/checks visibility** | None today — `get_pr_status()` doesn't fetch check-run status at all | Native — GitHub already blocks/warns on failing checks in its own merge UI |
| **Audit trail** | Already unified: `audit_log()` fires on every gate resolution (`gates.py:144-145`), correlated to the assessment | GitHub's own PR history is the audit trail; AgentIT would need `get_pr_status()` polling (already exists) to reconcile it back into the Ledger |
| **Consistency with existing pattern** | Extends a pattern already live for every GitOps-registered delivery | Introduces a second, inconsistent pattern (some PRs merge in-app, some don't) unless applied everywhere |

### Recommendation

**Keep and extend UI-merge as the default, but only for the merge action
itself — do not build a full diff/review UI to compete with GitHub's.**
Concretely:

1. **UI-merge is not "just extending an existing pattern" for a
   trivial reason — it's the *correct* extension**, because the thing a
   human is actually approving at that gate is not "is this diff good"
   (GitHub already shows that, and re-implementing a diff viewer well is a
   real, non-trivial UI investment this codebase has never made) — it's
   **"has this PR's CI passed and have I decided to let Argo apply it,"**
   which is exactly the kind of state/decision AgentIT's gate model
   already exists to track (`gitops-pr-pending`, correlated to an
   assessment, audited, resolvable idempotently via `s.resolve_gate()`'s
   atomic claim). Keeping the merge action in the portal keeps the whole
   delivery lifecycle (deliver → gate → merge → verify) inside one audit
   trail and one Ledger stream, which sending humans to GitHub would break
   — `get_pr_status()`'s polling would then be the *only* way AgentIT ever
   finds out a merge happened, reintroducing exactly the "no visibility
   into merge" gap `docs/unified-apply-flow.md:394-401` names as a current
   problem for the parts of the flow that *aren't* gated.
2. **But close the one real gap this pattern has today**: before
   surfacing "Merge" as a one-click action, fetch and show the PR's
   real CI/check-run status (`GET /repos/{owner}/{repo}/commits/{sha}/
   check-runs` — a new, small addition to `github_pr.py`, structurally
   identical to `get_pr_status()`) and a lightweight files-changed list
   with real content (already have `get_onboarding()`'s file content in
   the store — no new fetch needed, just render it). This is *not* "build
   a full diff/review UI" — it's "don't let a human merge blind to CI
   status," which is a much smaller, bounded addition.
3. **Always keep a "View on GitHub" link alongside the in-app Merge
   button** (cheap, already have the `pr_url`) for the human who wants
   GitHub's full review UI, inline comments, or wants to request changes
   rather than merge outright — UI-merge should be the fast path for the
   common case ("looks good, ship it"), not the only path.
4. **Do not extend this to non-gated PRs** (Per-Agent PRs, source-repo
   patches) until they're folded into the router per §3/consolidation-
   proposal Part 2 — merging them in-app today would mean merging PRs that
   never went through `classify_file()`'s secret-block/placeholder checks,
   which is a strictly worse position than today's "at least a human sees
   the GitHub PR before merging it there."

---

## 6. Notified once merged AND once rolled out

### What exists today

- **"Merged" detection**: `get_pr_status()` (`github_pr.py:38-105`) already
  distinguishes `merged`/`open`/`closed`/`unknown` via the GitHub REST API
  (`merged_at`/`merged` fields) — this is real, working code. But it is
  **pull-only, called only from two page-load call sites**
  (`onboarding_history`, `assessments.py:559-580`; `onboard_results`,
  `assessments.py:887-892`) — there is no background poller and no GitHub
  `pull_request` webhook subscription anywhere. A merge that happens while
  no human has that specific page open produces zero AgentIT-side signal
  until someone next loads it.
- **"Rolled out" detection is further along than "merged" detection**:
  `DriftDetector.detect_once()` (`drift_detector.py:33-126`) already polls
  every Argo CD `Application`'s `status.sync.revision` every 10 minutes
  (default `interval`), and `_maybe_close_gitops_delivery()`
  (`drift_detector.py:148-185`) already cross-references that revision
  against `list_pending_gitops_deliveries()`'s recorded commit SHAs,
  automatically kicking off `verify_and_close_delivery()`
  (`delivery.py:342-378`) the moment a delivery's commit actually syncs.
  This is real, already-wired, already-closing-the-loop code — not a gap.

### What's missing

1. A **merge-detection trigger that isn't page-load-dependent** — either
   a GitHub `pull_request` webhook (mirrors the existing `github-push`
   webhook's shape almost exactly, `webhooks.py:153-164`'s
   `X-GitHub-Event` dispatch already has the pattern) registered on the
   infra repo (today's `ensure_webhook` call only ever targets the app's
   own repo), or a lightweight background poll over `list_pending_gitops_
   deliveries()`'s PR URLs (the same store method `drift_detector.py`
   already calls) on a timer, calling the already-existing `get_pr_status`.
2. **A user-facing notification once either of these fires** — this is
   the same gap as §4: detecting the event is close to done (rollout) or a
   small addition (merge); *telling the user* still requires the outbound
   channel neither exists today.

### What would need to change

- Add a `pull_request` case to `webhook_github_push` (rename/split it, or
  add a sibling route) that fires on `action: "closed", merged: true` —
  register it on the infra repo the same way `ensure_webhook` already
  registers push webhooks on the app repo, and have it call
  `verify_and_close_delivery`'s merge-side counterpart (there isn't one
  yet — today only `DriftDetector`'s sync-revision match triggers verify;
  a merge event should mark the delivery "merged, awaiting sync" as a new,
  distinct status rather than skip straight to "verified").
- Once that status exists, the notification in §4's outbound channel has
  two natural trigger points instead of one: "merged, awaiting rollout"
  and "rolled out, verified" — matching the product owner's "once merged
  AND once rolled out" literally, rather than collapsing them into one
  event.

---

## 7. AgentIT verifies the fix actually worked

### What exists, and exactly what it checks

`property_verifier.py::verify_all_properties()` (`property_verifier.py:
151-158`) is fully implemented, has a clean registry pattern
(`PROPERTY_VERIFIERS`, lines 143-148), and is reachable today **only** via
`GET /api/assessments/{assessment_id}/verify`
(`assessments.py:1234-1266`) — a JSON API endpoint with the docstring's own
admission: *"this is a standalone API endpoint, not (yet) wired into the
automatic onboarding/apply path."* No template, button, or link in the
entire portal calls it. Confirmed: dead code from a UI standpoint, exactly
as flagged.

**What it actually verifies is narrower than "the fix worked," and this
matters for how it gets wired in:** its four registered checks
(`_verify_network_isolation`, `_verify_rbac`, `_verify_autoscaling`,
`_verify_monitoring`) all run against the **generated manifest content
itself** (`yaml.safe_load_all(f.content)` on each `GeneratedFile`) — they
answer *"does this proposed manifest set contain a NetworkPolicy/RBAC
triad/HPA/ServiceMonitor,"* a **static, pre-delivery structural check**,
not a live-cluster, post-deployment check. It never talks to the cluster,
never re-runs the original analyzer/check that produced the finding, and
never confirms anything is actually running.

### Is this the mechanism step 7 needs?

**Partially, and only for one specific meaning of "worked."** There are
two different things "verify the fix actually worked" could mean, and the
codebase has different-strength coverage for each:

1. **"Does the generated manifest structurally satisfy the property it
   claims to fix?"** — `verify_all_properties()` answers exactly this,
   and is a real, if narrow (4 hardcoded property types, not general),
   answer. Wiring it in is genuinely just "add a button/step that calls
   it" — the hard part (the checks themselves) is done.
2. **"Is the fix now live and functioning in the actual cluster?"** —
   this is what `verify_slos()` (`remediation_loop.py:37-76`) and
   `verify_and_close_delivery()` (`delivery.py:342-378`) already do, for a
   **different, narrower signal**: SLO breach/no-breach over a fixed 60s
   window post-delivery, not "does the specific finding this fix targeted
   still reproduce." Neither of these re-runs the original failing
   analyzer/check against the live, deployed state.
3. **"Does the finding that triggered this fix no longer show up on
   re-assessment?"** — this is the closest thing to "close the loop" the
   codebase has, and it's exactly what a subsequent Assess already does
   structurally (analyzers/checks re-run fresh against the (by-then-
   updated) repo) — but nothing today explicitly correlates "finding X
   from assessment N" to "is finding X present in assessment N+1" as a
   named verification outcome; a human (or nothing) currently has to
   notice the score/finding-list changed.

None of the three is wrong or redundant with the others — they're three
real, different verification questions, and the vision's "verify fixes are
rolled out and working as expected" plausibly wants all three, chained.

### What would need to change

1. **Wire `verify_all_properties()` into the actual onboarding/delivery
   flow** — the lowest-effort, most literal fix for "dead code": call it
   right after generation (in `_run_onboarding_job`,
   `assessments.py:583-669`) or right before Deliver, and surface its
   `passed`/`summary()` output (already computed, already structured) as a
   real card on Onboard Results, the same way Dry Run's status chip works
   today. This closes half of step 7 with no new detection logic — only
   new call sites and a template.
2. **Extend `verify_and_close_delivery()`'s SLO-only check** to also
   re-run the specific finding/check that motivated the fix (the
   dispatcher already knows `category`/`description` at generation time,
   `assessments.py:456-475`) against the post-deployment state — this is
   newer, real work, not wiring: it needs whichever analyzer/check
   produced the original finding to be re-invokable narrowly (single
   category, single app) rather than only as part of a full re-assessment.
3. **Correlate assessment N's findings to assessment N+1's** so "the fix
   worked" can eventually be stated as a fact ("this finding, present in
   assessment N, is absent in assessment N+1") rather than inferred from a
   score delta — `assessment_diff.py::diff_assessments()` already exists
   and is already used for exactly this in the re-assessment webhook
   (`webhooks.py:242-247`) to detect `diff.auto_fixable` findings; the
   missing piece is using that same diff, after a delivery, to positively
   confirm removal of the specific finding a delivery targeted, not just
   to discover new ones.

---

## 8. "Next Assess will run this loop again"

### What exists today

**This already, partially, happens today** — for the one case that isn't
a human clicking "Assess" fresh: `webhook_github_push`
(`webhooks.py:153-282`) already re-assesses on every push to a managed
repo's default branch, diffs the new assessment against the previous one
(`assessment_diff.diff_assessments`, lines 237-243), and, **when `auto_
mode` is on**, automatically dispatches a fix for every `diff.auto_
fixable` finding via `RemediationDispatcher` (lines 251-259) — with a
3-strikes rejection-count guard (`s.get_rejection_count`, line 255) so a
repeatedly-rejected finding-category stops being auto-retried. This is a
real, live "the loop runs again automatically" implementation — just
scoped to git-push-triggered re-assessment, and to *dispatching a fix*
(generation), not (yet) delivering it — dispatch here stops at
`dispatcher.dispatch()`, it never calls `AutoMode.execute()` or `route_
and_deliver()` for these auto-fixable findings today.

### What's missing

- **A human-initiated "Assess" (or "Re-assess") click has no equivalent
  chaining** — `assess_submit()` only auto-chains into onboarding via the
  explicit `continue_onboard` flag (§2), and even that never continues
  past onboarding into delivery; a human always has to separately act on
  Onboard Results.
- Nothing today explicitly states, in the UI, "the next Assess will
  re-run this whole pipeline" — the closest existing copy is `assess_
  progress.html:37-38`'s "*After scoring, onboarding will start
  automatically*," which is accurate only for the `continue_onboard`
  case and doesn't mention delivery at all.

### Does this already naturally re-trigger, once §1-3 are automated?

**Yes, once §1 (mandatory GitOps + bootstrap fix), §2 (assess auto-chains
to onboard for every app, not just `ever_onboarded`), and §3 (onboarding
auto-chains to delivery via default auto-mode) are all in place, step 8
requires no new orchestration of its own** — every Assess, human-initiated
or webhook-triggered, would already walk assess → onboard → deliver →
gate → (once §6 exists) verify, because each stage would already
unconditionally hand off to the next. `webhook_github_push`'s existing
auto-fix-dispatch behavior (lines 251-259) is the one piece of *proof* this
chaining pattern already works end-to-end for at least the assess→generate
half, in production, today — it just needs the same treatment `RemediationLoop.trigger()` already gives the fully-autonomous path, applied
to the human-initiated one.

### What would need to change

1. Nothing new to build *specifically* for step 8 if §1-3 land as
   described — this is the one step in the vision that resolves as a
   consequence, not a separate deliverable.
2. **One piece of UI copy work remains regardless**: once the chain is
   real, Onboard Results / Assessment Detail should say so explicitly
   (the way `assess_progress.html` already does for the narrower
   `continue_onboard` case today) — "Next Assess will re-run this
   pipeline automatically" is cheap, accurate, user-facing honesty once
   the underlying chain exists, and should not be stated before it does
   (this codebase's own convention, per multiple templates checked this
   session, is to never claim automation that isn't real — see
   `register_gitops`'s carefully-worded success message, §1).

---

## Sequencing: superset of the two existing proposals, with one required insert

**This vision is a superset/evolution of the consolidation proposal and
`docs/unified-apply-flow.md`, not a reason to revise either.** Every
mechanism the vision needs is already named in one of those two docs
*except* the §1 bootstrap-circularity bug (newly found this session) and
the outbound-notification channel (§4/§6, named as a gap by
`docs/unified-apply-flow.md` but never designed). Recommended build order,
each phase shippable and independently useful:

1. **Fix the GitOps bootstrap circularity (§1).** This is a small,
   surgical fix to `route_and_deliver()`'s registration check
   (`delivery.py:473-480`) and is a **blocking prerequisite** for
   everything else in this vision — every later phase either delivers via
   GitOps by default or entirely, and none of that can work correctly
   until a brand-new app can actually reach `registered=True` at all. Ship
   this alone, first, regardless of what else gets prioritized.
2. **Ship the consolidation proposal's Part 2 (fold Per-Agent PRs into
   Deliver; GitOps default with opt-out) and Part 3 (LLM manifest-review
   gate) largely as designed.** These are already-scoped, already-
   reviewed pieces of work that the vision's §1/§3 sit directly on top of.
   The one change this analysis motivates: build the manifest-review gate
   as **mandatory on the auto-mode path from day one** (per §3 above),
   since Phase 4 below makes auto-mode the default far sooner than the
   original proposal assumed it would be exercised.
3. **Extend `continue_onboard` to every app (§2), and wire `AutoMode.
   execute()` (or equivalent) as onboarding's automatic next step (§3),
   behind a single, clearly-labeled "fully autonomous onboarding" setting**
   — this is the point at which the product owner's "no difference between
   assessment and onboarding" and "PRs automatically created" become
   literally true, and it should ship *after* Phase 2's manifest-review
   gate, not before, per the safety analysis in §3.
4. **Add the outbound notification channel (§4) and the infra-repo
   `pull_request` webhook + merge-status tracking (§6)** — genuinely new
   work, no existing partial implementation, but small and additive
   (`EventPublisher` already has the right shape as a hook point). Do this
   once phases 1-3 are live, since before then there's materially less to
   notify anyone about.
5. **Wire `verify_all_properties()` into the delivery flow and extend the
   verify tail to re-check the originating finding (§7)**, then update
   Onboard Results / Assessment Detail copy to state the now-real closed
   loop (§8). This is deliberately last: it's the "does the whole loop
   actually work" proof, and it's most useful once every earlier stage in
   the loop is itself real.
6. **UI-merge extension (§5's CI-status/files-changed addition)** can ship
   in parallel with any of the above once Phase 2's gate model is in place
   — it has no dependency on phases 3-5.

This sequencing deliberately does **not** ask to revise either existing
proposal's own scope — it inserts one new prerequisite fix ahead of both,
and treats the rest of the vision as "run those two proposals to
completion, then keep going in the same direction they already point."

---

## Final status (2026-07-17): what actually shipped, phase by phase

Written after the fact, against the real commit history, not as a rewrite
of the analysis above — every claim below cites a real commit hash on
`main`. Where reality shipped something *different* from what a section's
"What would need to change" recommended (it does, twice — see §2/§3 and
§3's LLM-gate point below), that's called out explicitly rather than
smoothed over. **This is an honest accounting, not a rubber stamp**: three
real gaps (§4, §5's CI-status addition, §6's merge webhook, §7's items 1-2)
are still open below, exactly as this document originally found them — this
session's work did not touch them, and no later commit has either.

### §1 — "Setup Argo is mandatory, no more direct updates to cluster": **SHIPPED**

- Bootstrap-circularity fix (the blocking prerequisite this doc named):
  `a6365b7` — a known infra repo now always resolves to a real commit
  attempt, not `MECHANISM_DIRECT_APPLY`, closing the "can never reach
  `registered=True`" loop.
- Direct Apply removed as a concept entirely, in 5 separately-tested steps:
  `90b95b2` (GitOps registration made mandatory on Assess), `855d438`
  (Direct Apply removed from the onboarding/delivery UI and mechanism
  resolution), `8654df8` (the now-unreachable `cluster-conflict-review` gate
  type removed), `2b8ef2f` (`AutoMode`'s direct-apply branch and the
  auto-mode allowlist removed), `423b508` (`cluster_apply.py`'s dead
  direct-apply code paths removed).
- `cluster-admin-review` (the CI/CD-shared-namespace gate) was kept as a
  real, reachable exception, exactly per this doc's own §1 recommendation
  — confirmed still live and untouched by the removal above; it answers a
  different question ("does this service account have shared-namespace
  RBAC") than "should this fix go through GitOps."

### §2 ("no difference between assessment and onboarding") / §3 ("PRs automatically created"): **SHIPPED — via a different, arguably safer mechanism than this doc recommended**

- `f215d13` made `continue_onboard` the unconditional default for *every*
  `Assess`, not just `ever_onboarded` apps as it was when this doc was
  written — stronger than this doc's own §2 recommendation (which floated a
  settings-gated flag). Concretely: `assess_progress()` now auto-launches
  onboarding the instant an assess job completes, for every app, with no
  human step at Assessment Detail in between (`continue_onboard=0` remains
  a real opt-out Form field; nothing in the app sets it today).
- `64c3ecc` (with a real bug it caught and fixed along the way, `09739b4`)
  chains onboarding straight into an automatic Dry Run → Deliver, so the
  full path is now **Assess → Onboard → Dry Run → Deliver (PR opened)**
  with zero human clicks before a PR exists — literally the product
  owner's "no difference between assessment and onboarding... PRs are
  automatically created," for every app, every time.
- **The named tradeoff was, in fact, accepted, not separately re-litigated**:
  §2's "real decision this forces" — a human no longer sees raw findings
  at Assessment Detail before fixes are generated for all of them — is now
  genuinely true for every app, not just a hypothetical. This session's own
  Phase 5 work (see below) is a direct, partial answer to the visibility
  this removes: a per-app "what happens next" fact, so a human isn't flying
  blind between the automatic Assess and the eventual PR.
- **What did *not* ship, and is worth naming precisely**: this did **not**
  go through `AutoMode.execute()`'s LLM `classify_action` safety check, and
  the consolidation proposal's Part 3 LLM manifest-review gate (§3's "load-
  bearing once auto-mode is the default" recommendation) was never built —
  grep confirms no `manifest-review`/`manifest_review` gate type exists
  anywhere in the codebase. The auto-chain instead calls `route_and_
  deliver()` directly. This is arguably *lower* risk than the scenario this
  doc worried about, not higher: Direct Apply's total removal (§1) already
  eliminated the specific failure mode motivating the LLM gate (an
  LLM-classified-"safe" fix mutating a cluster directly with no human in
  the loop) — every auto-chained delivery lands on a GitOps PR, and a human
  still merges every one. But "does this manifest actually fix the finding
  it claims to" is still answered by nothing before that PR opens; only
  §7's item 3 (below) catches a wrong fix, and only *after* a human already
  merged it.
- "Per-Agent PRs" (the one delivery-shaped mechanism with zero of the
  router's safety checks) was **not** folded into the router — confirmed
  still a separate, ungated route (`create_agent_prs_route`). Unchanged
  since this doc's §3 named it; still the least-safe path in the app.

### §4 (outbound notification that a PR needs attention): **NOT SHIPPED**

No Slack/PagerDuty/email/webhook-out integration exists anywhere in the
codebase today — re-confirmed this session (a repo-wide search for
`slack`/`pagerduty` turns up only the *assessed app's own* observability
analyzer, unrelated to AgentIT's own outbound signal). "Notified" still
only happens if a human is already looking at Fleet, Ledger, or Assessment
Detail. This session's Phase 5 work (below) makes the in-app fact more
specific and honest, but does not add a push channel — that remains
exactly the gap this doc named, unaddressed.

### §5 (UI-merge vs. GitHub-merge): **Recommendation followed by default; the one concrete addition it asked for was not built**

UI-merge (`gitops-pr-pending` gate approval) remains the live pattern, as
recommended — no regression, no competing pattern introduced. But the
specific, bounded addition this doc asked for — fetch and show the PR's
real CI/check-run status before offering a one-click merge — was **not**
built: no `check-runs` (or equivalent) call exists in `github_pr.py` today.
Still open.

### §6 (notified once merged AND once rolled out): **"Rolled out" detection unchanged (already real); "merged" detection and both notifications: NOT SHIPPED**

`DriftDetector`'s Argo sync-revision poll (the "rolled out" half) was
already real when this doc was written and is unchanged. The "merged" half
is still page-load-only — no `pull_request` webhook was ever added on the
infra repo, and no notification exists for either milestone. Unchanged gap.

### §7 (AgentIT verifies the fix actually worked): **Item 3 SHIPPED (this is what "Phase 3/4" means below); items 1-2 NOT SHIPPED**

This doc named three different, real "did it work" questions (§7). Only
the third shipped:

1. *"Does the generated manifest structurally satisfy the property it
   claims to fix?"* (`verify_all_properties()`) — **still dead code from a
   UI standpoint**, reachable only via `GET /api/assessments/{id}/verify`
   — re-confirmed this session, unchanged since this doc was written.
2. *"Is the fix now live and functioning in the cluster?"* — SLO-window
   verification (`verify_and_close_delivery()`) is unchanged; still never
   re-runs the specific originating check against live state.
3. *"Does the finding that triggered this fix no longer show up on
   re-assessment?"* — **this is what shipped**, as this session's two
   prerequisite phases (using this effort's own, `§7`-local numbering,
   distinct from the doc-level "Sequencing" phases 1-6 above — both
   numbering schemes exist in this codebase's history and are not the same
   axis):
   - **Phase 3 (correlation), `bc28bee`**: `route_and_deliver()` now
     records which finding(s) a delivery targeted
     (`deliveries.target_findings_json`); `delivery.correlate_delivery_
     finding()` answers resolved/still-present/pending against a later
     assessment's real current finding set; `webhook_github_push` checks
     every pending delivery on every push-triggered re-assessment
     (`check_pending_delivery_verifications()`), persisting
     `deliveries.finding_resolution` and logging a real event either way.
   - **Phase 4 (bounded auto-escalation), `ece2e1c`** (landed alongside
     `bc28bee`, dedicated test coverage + one real bug fix in this commit):
     a confirmed still-present finding, under `auto_mode`, re-dispatches a
     fresh attempt below `FINDING_ESCALATION_THRESHOLD` (3) confirmed
     failures (`store.get_finding_failure_count()`), or escalates to a real
     `finding-unresolved-escalation` gate at/above it — the loop provably
     stops rather than retrying an identical fix forever.

### §8 ("next Assess will run this loop again"): **SHIPPED**

This doc predicted step 8 "requires no new orchestration of its own" once
§1-3 landed — confirmed true: every Assess now walks Assess → Onboard →
Dry Run → Deliver automatically (§2/§3 above), and `webhook_github_push`'s
pre-existing re-assess-on-push behavior (unchanged) closes the loop for the
push-triggered case. The one piece of UI copy work this doc flagged as
"remains regardless" — telling a human, honestly, what happens next instead
of leaving them to infer it — is **Phase 5, this session's own work**:

- **Phase 5 (a real "what happens next" fact per app)**: `delivery.get_
  next_action_state()` turns Phase 3/4's data into one of four honest
  states for a given app — an open escalation gate ("Needs your review"),
  a bounded auto-retry already in flight ("Retry N of 3"), an ordinary
  first-time pending finding-check ("Awaiting verification — will check on
  next push to `<repo>`"), or, when nothing is pending or failing, a
  plain statement that nothing re-checks a clean app on any schedule —
  never a fabricated cadence. Surfaced on Fleet (a compact per-row badge,
  omitted entirely for a genuinely clean app) and Assessment Detail (a
  fuller header-area sentence, including the honest "no scheduled
  re-check" text for a previously-onboarded clean app). Backend + unit
  tests: `b3415da`. Fleet + Assessment Detail UI + rendering tests:
  `b4204a1`.

### What's genuinely still open, in one place

For anyone picking this back up: §4 (outbound notification — no channel
exists at all), §5's CI-check-status addition, §6's merge webhook +
either notification, and §7's items 1-2 (wiring `verify_all_properties()`
in; extending the verify tail to re-check the originating check against
live cluster state, not just re-assessment) are the real remaining gaps —
each independently additive, none blocking anything else, exactly as the
original "Sequencing" section above scoped them (its phases 4-6). The
consolidation proposal's Part 2 (fold Per-Agent PRs into the router) and
Part 3 (LLM manifest-review gate) were also never built — see §2/§3 above
for why the LLM gate specifically is lower-priority than originally
assessed now that Direct Apply is gone, not why it stopped mattering
entirely.
