# UI/UX Redesign Proposal

**Status: design only, not yet built.** Same posture as
`docs/unified-apply-flow.md` — a proposal for the repo owner to review
before any of it lands as code. Nothing in this doc has been implemented.

This doc answers the "the entire UI feels disconnected" complaint with a
concrete IA/journey audit, not aesthetics. It builds on, and does not
duplicate, `docs/unified-apply-flow.md`'s `route_and_deliver()` work — that
doc solved *which mechanism delivers a change*; this doc is about *which
page/button a human uses to trigger and observe that delivery*, and why
today's answer to that question is scattered across pages that don't know
about each other.

## 0. The 7th path, confirmed

Tonight's investigation was right: `fix_finding()`
(`routes/assessments.py:307-363`) still calls
`apply_manifests_to_cluster()` directly (line 341), not
`route_and_deliver()`. A repo-wide grep for every caller of
`apply_manifests_to_cluster`/`apply_with_verification`/`route_and_deliver`
confirms this is the *only* remaining direct caller outside
`cluster_apply.py`'s own definitions — every other path
(`assessments.py::apply_to_cluster`, `assessments.py::deliver`,
`gates.py::resolve_gate`, `automode.py::execute`) already goes through
`apply_with_verification()` or `route_and_deliver()`.

Concretely, `fix_finding()` today:

1. Dispatches the fix generator (`RemediationDispatcher.dispatch()`) — correct, keep.
2. Runs a raw, unaudited `apply_manifests_to_cluster(dry_run=True)` (lines 339-344) — **wrong**, this is the bypassed path.
3. Redirects to `/assessments/{id}/onboard-results` (line 355-358), where the human is told *in the flash message* ("Review below and apply to cluster or create a PR") to use buttons — **"Apply to Cluster" and "Create PR" don't exist as buttons on that page any more** (`onboard_results.html` only has "Dry Run" / the single "Deliver" button / "Per-Agent PRs" / "Download") — stale copy left over from before the unified-apply-flow consolidation.
4. On that same onboard-results page, the human then clicks the real "Deliver" button, which **does** call `route_and_deliver()` (`assessments.py:675`).

So the dry-run apply in step 2 is not just unrouted — it's **dead work**: its result is saved (`save_apply_results`, line 344) and immediately superseded by whatever `route_and_deliver()` does in step 4, and it silently reintroduces the exact direct-apply-into-a-pruned-namespace risk `route_and_deliver()` exists to prevent, for a GitOps-registered app whose finding gets "Fixed."

**The fix is small, not a redesign**: delete `fix_finding()`'s direct
`apply_manifests_to_cluster` call (lines 338-344's apply block down to just
the `save_remediation`/`log_event` bookkeeping), and update the redirect's
flash-message copy to say "Review below and Deliver" instead of
"apply to cluster or create a PR." `fix_finding()` becomes purely a
**generation** step, matching what "Onboard This App" already is for a
whole plan — see §3 for why this is the right shape, not a stopgap.

**Second bug in the same area, also a router-consolidation miss**: the two
places that decide "does this finding get a Fix button" have drifted:

- `assessment_detail.html:126` (Findings tab, per-finding) uses
  `fixable_categories`, computed server-side from
  `remediation/registry.py::lookup(f.category)`
  (`assessments.py:263-264`) — the correct, single source of truth.
- `assessment_detail.html:182-187` (Remediation Plan table) uses an
  **independent, template-only keyword substring match** against
  `item.description | lower` (`'container' in cat or 'network' in cat or
  ...`), and posts `category=item.dimension` — a *dimension* name
  (`security`, `compliance`, …), not a finding *category*
  (`container`, `network`, …) — to the same `/fix` endpoint that expects a
  category. This works today only because `RemediationDispatcher.dispatch()`
  apparently tolerates dimension-shaped input for some categories; it is
  not the same logic and will silently diverge the next time either list
  changes. Fix: compute `fixable_categories`/an equivalent `is_fixable(item)`
  once, server-side, from the same `lookup()` call, and pass it to both
  loops instead of re-deriving visibility in Jinja.

## 1. The real current journey (concrete, page-by-page)

**Task: get a low-scoring app hardened and live, today**, walked with real
page names and clicks:

1. **Fleet** (`/`) → click app name → **Assessment Detail** (`/assessments/{id}`).
2. **Assessment Detail**, Findings tab → for each fixable finding, click **Fix** (confirms "Generate a fix for this finding?") → server dispatches + (today) does a stray raw dry-run apply → redirects to **Onboard Results** (`/assessments/{id}/onboard-results?fix_generated=...`).
   - *Or*, back on Assessment Detail, click **Onboard This App** instead (a completely separate action, running the *full* multi-agent orchestration pipeline for every dimension, not just one finding) → same **Onboard Results** page.
3. **Onboard Results** — a human now sees a `delivery_confirmation` banner ("AgentIT will: …"), and must click **Dry Run** first (a separate click, a separate page state), read the result, then click the single **Deliver** button (labeled "Apply to Cluster" or "Commit & Open PR" depending on GitOps registration) and confirm a modal.
4. If the app is GitOps-registered, Deliver opens a PR. The human leaves AgentIT entirely, merges on GitHub, and — if any *other*, unrelated finding on this same app auto-mode or a webhook flagged — a gate may now be sitting on **Gates** (`/gates`), a fourth page, with **no indication on this card of which app it's for** beyond whatever happens to be in its free-text `summary` (most gate types don't mention the app name at all — see §2).
5. If a CI/CD manifest targeted a shared operator namespace, there's a `cicd_gate` warning banner back on Onboard Results pointing to **Gates** again, for a `cluster-admin-review` gate — a fifth context switch, this time for a different human (whoever holds elevated RBAC) who has to go find it in the same undifferentiated queue as every app-owner's ordinary approval gates.
6. To confirm the fix actually helped, the human goes back to **Assessment Detail**, triggers a re-assessment from **Fleet**, and compares scores — no page currently says "this finding was fixed and verified" in one place; that state is split across Onboard Results' delivery history table, the Findings tab (which still shows the *original* finding until the next assessment), and Gates' resolved-gates table.

That's (at minimum) **5 distinct pages** for one task, several of which
require the human to already know what the other page will show before
navigating there (e.g., "there might be a gate for this app now, go check
Gates"). This is the concrete substance behind "the UI feels disconnected"
— it's not a color/spacing problem, it's that the *task* is scattered
across pages that don't reference each other.

## 2. Should Gates dissolve into Fleet? A real recommendation, not a hedge

**What is a "Gate," conceptually, now that `route_and_deliver()` exists?**
Before the unified apply flow, a gate meant "something is about to touch
the cluster and a human must decide first" — a single, coherent concept.
After it, `route_and_deliver()` handles the *routine* case entirely on its
own (direct apply when safe/not-registered, or an autonomous commit+PR when
GitOps-registered) with no gate at all. A gate today only exists for one of
these **structurally different** reasons:

| Gate type | Who resolves it | Why a gate exists at all |
|---|---|---|
| `finding-{category}` | The app owner | A per-finding fix was generated via webhook dispatch and needs review before delivery |
| `auto-mode-review` | The app owner | AutoMode's LLM safety gate said "not confident enough to auto-apply" |
| `dry-run-failed` | The app owner | AutoMode's own dry-run failed |
| `gitops-pr-pending` | The app owner (or whoever merges PRs for that app) | AutoMode opened a PR autonomously but a human must merge it — never auto-merge |
| `auto-mode-scope-review` | The app owner | Manifests fell outside the configured auto-mode allowlist |
| `rollback-review` | The app owner | An SLO breach was detected after a recent apply; needs a human rollback decision |
| `cluster-conflict-review` | The app owner (usually) | A server-side-apply field-manager conflict (HTTP 409) needs explicit force |
| `cluster-admin-review` | **A different person** — whoever holds elevated RBAC, not the app owner | CI/CD manifests target a shared operator namespace (`openshift-gitops`/`openshift-pipelines`/etc.) this service account can't apply to by default |

**Seven of eight gate types are app-owner-scoped and per-app in every
practical sense** — every one of them is keyed by `assessment_id`, i.e. one
specific app. **One gate type, `cluster-admin-review`, is the only one
built for a genuinely different audience** — this is exactly the gap
`docs/unified-apply-flow.md` named and explicitly left unaddressed
("Deliberately not addressed #2: the `cluster-admin-review` gate's actual
UI/RBAC model").

There's also a concrete, current defect that settles the "is Gates doing
its job as a queue" question on its own: **`gates.html` never displays
which app a gate belongs to.** `list_gates()`/`list_all_gates()`
(`store.py:883-894`, `store_pg.py:868-876`) are flat `SELECT * FROM gates`
queries with no join back to the assessment/app; the template
(`gates.html:14-24`) shows `gate_type`, a timestamp, and a free-text
`summary` — and of the eight `create_gate()` call sites
(`automode.py:347,373,381,406,461,510`, `webhooks.py:406`,
`delivery.py:426`), only `slo_tracker.py:120`'s `rollback-review` summary
happens to mention the app name in its message text. Every other gate card
is, today, **anonymous** — a human reviewing five pending gates cannot tell
which apps they're for without clicking through to "Preview Files" on each
one. A page whose core job is "here's a queue of things needing your
attention, organized by what they're about" and which doesn't display the
"what they're about" field is not really serving as a coherent queue; it's
an unsorted inbox.

### Recommendation: split by audience, don't uniformly merge or uniformly keep

1. **Fold the seven app-owner gate types into Fleet + Assessment Detail — dissolve them as a standalone global page.** Concretely:
   - **Fleet row**: add a "Needs Action" badge/column (e.g. a small pill: "2 pending" in the accent color) next to each app that has pending gates — computed from a `GROUP BY assessment_id` count, cheap and already-available data.
   - **Assessment Detail**: add a 4th tab, **Actions** (alongside today's Overview / Findings / Timeline), showing that app's pending gates with the exact same Approve & Deliver / Reject / Dismiss UI `gates.html` has today — reuse the same partial/macro, don't reinvent it.
   - This directly answers "which app is this for" — the gate literally lives on that app's page, no separate lookup needed — and it means a human already on an app's page (which is where every real task starts, per §1) never has to context-switch to a different top-level page to finish approving something for the *same app they're already looking at*.
   - Nav badge: the existing `pending_gates` count in `base.html`'s nav (which today has a real bug — it's referenced at `base.html:789` but no context processor actually supplies it; only `insights.py::get_fleet_insights()` computes it, so the badge is silently blank on every page except Insights) becomes the *fleet-wide* total shown on **Fleet**'s nav link instead of a now-removed "Gates" link — same signal, correctly wired, pointing at the page where each pending item actually lives.

2. **Keep exactly one small, separate surface for `cluster-admin-review`.** Not because "some gates deserve their own page" as a general principle, but because this is the one gate type whose resolver is a genuinely different person with genuinely different permissions than an app owner — merging it into a per-app Actions tab would require every cluster admin to know which of dozens of apps might have one pending, which is worse, not better. Concretely: a small **"Admin Review"** link, visible in nav only to identities the RBAC model actually grants elevated apply access to (or, if that's not resolvable from `X-Forwarded-User` today, shown to everyone but clearly labeled "requires elevated RBAC to resolve" — matching the honesty of the existing `_RBAC_HELP` messaging in `cluster_apply.py`) — a queue of exactly one gate type, cross-app, because that's the one case where "cross-app" is the correct shape for the audience, not an artifact of not having built per-app plumbing yet.

This is not a hedge between "merge" and "don't merge" — it's the
recognition that **"Gates" was never one concept**, it was two concepts
(per-app pending action; cross-app elevated-RBAC review) that happened to
share one table and one page because building two was more work up front.
Splitting them along the line that already exists in the data
(`gate_type == "cluster-admin-review"` vs. everything else) is a smaller,
more honest change than either "keep one global page" or "merge
everything into Fleet indiscriminately."

## 3. One verb: "Deliver." "Fix" and "Onboard" are triggers, not alternatives to it.

The task asks: should Fix (per-finding) and Deliver (per-plan) be the same
action at different granularities, or different actions that both route
through `route_and_deliver()`? **They should be different *trigger* verbs
that both terminate in the same *delivery* verb** — and that shape mostly
already exists today; it's one bad line of code (the fix_finding() direct
apply from §0) that breaks it, not a missing feature.

Recommended verb taxonomy, unambiguous per surface:

| Verb | What it does | Where it appears | Terminates in |
|---|---|---|---|
| **Assess** | Score a repo, no changes | Fleet ("Assess New Repo"), Fleet row ("Re-assess") | Nothing — read-only |
| **Fix** | Generate a remediation for **one finding** | Assessment Detail, Findings tab; Remediation Plan table | Redirects to Onboard Results; generates files only, does **not** apply/commit anything itself (§0's fix) |
| **Onboard** | Generate remediations for **every** applicable finding across all dimensions via the full agent/skill pipeline | Assessment Detail (top-level "Onboard This App") | Redirects to Onboard Results; generates files only, same as Fix |
| **Deliver** | The **one and only** verb that ever changes cluster or repo state — decides mechanism via `route_and_deliver()`, shows the same `confirmation_text()` before and inside the confirm modal | Onboard Results (single button, dynamically labeled "Apply to Cluster"/"Commit & Open PR"); Gates ("Approve & Deliver", for the 7 app-owner gate types that survive as an Actions-tab-embedded form) | Cluster apply, GitOps commit+PR, source-repo PR, or a new gate — per the router |

This makes the mental model exactly what the task asked for: **"Fix" and
"Onboard" are both just different-sized *generation* requests** (one
finding vs. every finding) that both land in the same place and both go
through the same terminal "Deliver" action — never two independent
apply-shaped verbs competing for the same click. Concretely, once §0's fix
lands, **`Deliver` is already the only verb in the entire codebase that
ever calls `route_and_deliver()`/`apply_with_verification()` from a
browser-originated action** (gate-approve's button literally says "Approve
& Deliver" already) — this section is naming and locking in a pattern
that's 90% real today, not inventing a new one.

**Also recommended — retire the two dead direct-mechanism routes.** A
repo-wide grep confirms `POST /assessments/{id}/apply`
(`assessments.py:606-649`, `apply_to_cluster`) and
`POST /assessments/{id}/create-pr` (`assessments.py:780-820`, `create_pr`)
are not linked from any template today — `onboard_results.html` only posts
to `/deliver`, `/create-agent-prs`, and the manifest-download `GET`. These
two routes are exactly the kind of extra "path to update an app" the task
asked to reduce: they still exist, still work if hit directly (a stale
bookmark, an old script, a curl command from a runbook that predates this
session), and still bypass nothing themselves (they call
`apply_with_verification` directly, not the router-aware taxonomy), but
they no longer serve any UI purpose. Recommend removing both routes (or, if
backward compatibility for some undiscovered external caller is a concern,
make them thin redirects to `/deliver` with `dry_run` preserved) — this is
a real reduction in "how many ways can I trigger a change," not just a UI
tidy-up, and it's the most surgical version of the seven-paths-down-to-N
goal available: two of the seven paths turn out to already be unused dead
code once you check.

**"Per-Agent PRs" and "Download" survive as distinct, smaller buttons** —
correctly, per `docs/unified-apply-flow.md`'s taxonomy: they're
structurally PR-only regardless of GitOps registration (source patches,
manifests-at-rest), not an alternative delivery mechanism for the same
cluster/app-config artifacts "Deliver" already owns. They should stay
visually secondary (already `btn-outline btn-sm`, correct) precisely so
they don't read as competing with the one real "Deliver" verb.

## 4. Making GitOps/Argo CD visible and preferred, not just structurally available

Today, GitOps registration status appears **exactly once** in the whole UI
— the `delivery_confirmation` banner on Onboard Results, which a human only
sees after already generating something to deliver. It's absent from:

- **Fleet** — the table has `deploy_status`/`deploy_namespace` columns
  (`fleet.html:49-50`, sourced from `fleet.py:67-90`'s Argo CD lookup or
  local apply-results fallback) that show *sync* state (`synced`/
  `applied`/`out-of-sync`/`not deployed`) but never *registration* state.
  An app can show `deploy_status = "applied"` (meaning: someone direct-
  applied it once) while also being GitOps-registered, or vice versa, and
  the Fleet table cannot currently distinguish those two very different
  situations at a glance.
- **Assessment Detail** — no mention anywhere; a human has to already know
  to click through to Onboard Results to find out.
- The **Assess New Repo modal** (`fleet.html:144-148`) has an optional
  "GitOps Infra Repo" field, but it's presented as one optional input among
  several, with no framing that this is the recommended, safer path — the
  UI treats "leave it blank" as an equally-fine default, when this
  project's own `CLAUDE.md` treats Argo CD as the correct default for
  *itself*.

### Recommendation

1. **Add a GitOps badge to the Fleet table**, next to (not replacing)
   the existing Deployed/sync badge — e.g. a small `badge-info` "GitOps"
   pill when `is_gitops_registered()` is true for that app (the same async
   check `route_and_deliver()` already performs; cache it the same 60s-ish
   window the skills cache uses, or piggyback on the existing Argo CD fetch
   `fleet.py:67-90` already does per-row, since both need the same
   `Application` lookup). Apps without it show a neutral "Direct apply"
   label instead of nothing — absence of a signal should never be the way
   a human learns "this app isn't GitOps-registered."
2. **Repeat the same badge on Assessment Detail**, next to the criticality
   badge at the top of the page (`assessment_detail.html:11`) — this is the
   page a human is on *before* they decide to Fix/Onboard, so "changes to
   this app go through PR + Argo sync" should be visible before they ever
   reach Onboard Results, not revealed only after they've already generated
   something to deliver.
3. **Nudge non-registered apps toward registration**, but as a suggestion,
   not a blocker (per the design doc's own stance that direct-apply remains
   fully valid for unregistered apps): a small, dismissible line under the
   "Direct apply" badge on Assessment Detail — "Not GitOps-registered. Apps
   under Argo CD get PR review + drift protection automatically" — linking
   to a lightweight action that pre-fills the existing "GitOps Infra Repo"
   registration path (today buried in the Assess-New-Repo modal, which
   doesn't help an app that's already been assessed) rather than requiring
   a full re-assessment with that field newly filled in. This makes GitOps
   the *visibly encouraged* path without forcing it — matching principle
   5/9 from the product's own design principles (human-in-the-loop,
   forgiving-by-design) rather than an auto-migration.
4. **Reframe the Assess-New-Repo modal's GitOps field**, from a neutral
   optional input to a labeled recommendation — e.g. move it above
   Criticality, relabel to "GitOps Infra Repo (recommended)", and change
   the helper text from "Leave blank to auto-create one" (already implies
   AgentIT will act) to something that states the tradeoff plainly: "Every
   change to this app will go through PR review and Argo CD sync instead of
   direct cluster apply. Leave blank and AgentIT will scaffold one for you."

## 5. Nav/IA proposal

Building on the precedent already set in `c274055` (pairing Agents↔Capabilities
and Schedules↔Settings as tabs, 9→7 top-level items), and on §2's gate
dissolution:

**Current nav** (`base.html:786-807`): Fleet, Gates (badge), Events, Health,
Insights, Decisions | *secondary:* Capabilities, Settings — **8 top-level
items** (6 primary + 2 secondary).

**Proposed nav:**

| Slot | Item | Change from today |
|---|---|---|
| Primary | **Fleet** | Unchanged link; now also carries the fleet-wide pending-actions badge (§2) that used to be Gates' |
| Primary | **Events** | Unchanged |
| Primary | **Health** | Unchanged |
| Primary | **Insights** | Unchanged |
| Primary | **Decisions** | Unchanged |
| Primary | **Admin Review** | New — replaces Gates; visible always, but only ever shows `cluster-admin-review` gates (§2); badge count reflects *that* count only |
| Secondary | **Capabilities** (Catalog/Registry/Self-Improvement tabs) | Unchanged grouping; see below for one tab-level fix |
| Secondary | **Settings** (Settings/Schedules tabs) | Unchanged |

**Net: 8 → 7 top-level items** (Gates removed as a distinct concept; Admin
Review is a much narrower replacement, and most gates now live where the
app they're about already lives). This is a smaller, more defensible cut
than "merge Gates into Fleet" taken literally would suggest — it's not
that Gates disappears into Fleet's existing columns, it's that *most of
what Gates was* turns out to already belong to a page (or a per-app tab)
that exists, and the sliver that's left (`cluster-admin-review`) is small
enough and different enough in audience to earn its own light-weight slot.

**One small, low-risk fix inside Capabilities**, closing the gap this
session's earlier flow-review already found: give **Self-Improvement**
parity with **Catalog**'s manual trigger. Catalog has "Research CVEs &
Generate Skills" (`POST /capabilities/learn`, `capabilities.html:14-16`).
Self-Improvement (`self_improvement.html`) has no equivalent — a human who
wants `capability-scout` to run right now (rather than waiting up to 24h
for its watcher tick) has no button, full stop. Recommend a matching
"Run Self-Improvement Scan" button posting to a new
`POST /capabilities/self-improvement/run` route that calls
`capability_scout.run_once()` synchronously (mirroring `/capabilities/learn`'s
existing synchronous-call shape, including its `haproxy.router.openshift.io/timeout: 200s`
Route annotation already accommodating a similarly long-running call) —
small, consistent with an existing pattern, and it's the one item in this
review that's a pure gap-fill rather than a consolidation.

**Not recommended**: renaming Capabilities' "Catalog"/"Registry" tabs, or
touching Insights/Decisions/Events/Health's structure — none of them were
named as a disconnection complaint, none of them have the "same task,
scattered across N pages" problem Fleet/Assessment Detail/Onboard
Results/Gates has, and changing them without a concrete journey-level
justification would be exactly the "aesthetics-only" reshuffling this
review was asked to avoid.

## 6. Deliberately not addressed

Matching this repo's own convention of naming scope boundaries rather than
hand-waving them:

1. **The RBAC model for the new "Admin Review" page** — who actually sees
   it, how elevated-RBAC identity is resolved from `X-Forwarded-User` (or
   whether it's shown to everyone with an honest "you may not be able to
   resolve this" caveat) is the same open question
   `docs/unified-apply-flow.md` already named and left open. This doc
   doesn't resolve it either — it only asserts the page should be small,
   separate, and gate-type-scoped once that RBAC question is answered.
2. **A pixel-level mockup of the Fleet "Needs Action" badge, the
   Assessment Detail "Actions" tab, or the GitOps badge's exact visual
   treatment.** This doc specifies placement, content, and data source,
   not markup — matching the level of detail `docs/unified-apply-flow.md`
   itself used for its own UI touchpoints ("Deliberately not addressed #6").
3. **Whether `deploy_status`'s sync-state badge and the new GitOps-
   registration badge should ever be collapsed into one badge.** They
   answer genuinely different questions ("is the last-known state in sync"
   vs. "does a change to this app go through PR+Argo at all") and keeping
   them visually distinct is deliberate, but the exact two-badge layout on
   a space-constrained Fleet row is a real design detail left for
   implementation.
4. **Retroactive migration of already-onboarded apps' Fleet rows** once the
   GitOps badge ships — same "worth a follow-up audit, not designed here"
   posture `docs/unified-apply-flow.md` already took for the underlying
   registration-detection logic (its "Deliberately not addressed #4").
5. **Whether "Insights" and "Decisions" should merge.** Both are read-only
   audit/analytics pages with some conceptual overlap (both surface LLM
   behavior), but neither came up as part of the "disconnected" complaint
   this session, and speculatively merging them without a journey-level
   complaint to justify it would repeat the mistake this review was
   commissioned to avoid.

## Summary for the repo owner

- **§0**: the 7th path is real and small to fix — delete `fix_finding()`'s
  direct `apply_manifests_to_cluster()` call and stale flash-message copy;
  fix the two independently-coded Fix-button-visibility rules to share one
  source of truth.
- **§2**: **don't uniformly merge Gates into Fleet.** Seven of eight gate
  types are genuinely per-app and belong on Fleet (badge) + Assessment
  Detail (new Actions tab). One (`cluster-admin-review`) is genuinely
  cross-app for a different audience and earns a small, separate "Admin
  Review" page — this is the first concrete answer to
  `docs/unified-apply-flow.md`'s own previously-unaddressed RBAC-UI gap.
- **§3**: **"Deliver" is already, almost, the one verb for "make this
  change happen"** — Fix and Onboard are *generation* triggers of different
  granularity that both feed the same terminal Deliver action; fixing §0
  is what makes this fully true instead of 90% true. Two now-unlinked
  routes (`/apply`, `/create-pr`) should be retired as an additional,
  concrete reduction in surfaces.
- **§4**: GitOps registration should be a **visible, persistent badge** on
  Fleet and Assessment Detail — today it's invisible until a human is
  already mid-delivery — plus a nudge (not a mandate) toward registering
  unregistered apps, consistent with this project's own stated GitOps-first
  posture for itself.
- **§5**: nav goes from 8 top-level items to 7 by retiring Gates as a
  standalone concept in favor of Fleet-embedded actions, replacing it with
  a much narrower Admin Review page — plus one small parity fix
  (Self-Improvement gets a "run now" button matching Catalog's).
