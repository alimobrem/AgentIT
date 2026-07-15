# The Ledger — design spec (buildable, not philosophy)

**Status: design spec, not yet implemented.** This is the next design
artifact in the same series as `docs/ui-redesign-proposal.md` (implemented),
`docs/next-gen-ux-concepts.md`, and `docs/ux-design-requirements.md`
(both still proposals). Everything below is grounded in the *actual current*
templates, routes, and `store.py` schema as of this writing — verified by
reading them directly, not designed against an imagined backend. No code
changes are made by this doc.

## 0. Resolving the real tension: increment, not replacement

**Verified current state** (read directly from the working tree, not
assumed): `docs/ui-redesign-proposal.md`'s 7 pieces are implemented.

- `base.html`'s nav is exactly: **Fleet, Admin Review, Events, Health,
  Insights, Decisions** (primary) + **Capabilities, Settings** (secondary,
  behind a divider) — 7 top-level items, no standalone "Gates" link.
  `GET /gates` is a 301 redirect to `/admin-review` (`routes/gates.py`).
- `admin_review.html` exists and is intentionally narrow — it renders only
  `cluster-admin-review` gates via `{% from "_macros.html" import gate_card %}`,
  exactly the shared macro, with the exact scoping comment from the design
  doc reproduced in its own template header.
- `fleet.html` has both the GitOps/Direct-apply badge pair and a "Needs
  Action" `pending_actions_count` badge per row (`routes/fleet.py::
  _attach_pending_actions()`, a `GROUP BY assessment_id` count that
  explicitly excludes `cluster-admin-review` gates).
- `assessment_detail.html` has the 4-tab shape (Overview / Findings /
  Actions / Timeline) with **Actions** rendering that app's own pending
  gates via the same `gate_card` macro Admin Review uses.
- The verb taxonomy (Assess / Fix / Onboard / **Deliver**) is in place —
  `gate_card`'s primary button literally reads "Approve & Deliver" and
  shows `gate.delivery_confirmation` inline before the confirm modal fires.

**The Ledger does not undo any of this.** It does not resurrect a global,
undifferentiated Gates page, and it does not touch the Admin Review /
app-owner audience split — that split is correct and stays. What the
Ledger changes is narrower and additive: it replaces the *scattering* of
this same underlying data (gates, events, deliveries, decisions) across
five separately-templated pages with **one queryable stream that can be
viewed scoped-to-one-app or fleet-wide**, reusing every existing action
(`gate_card`'s three buttons, `route_and_deliver()`, `resolve_gate()`) as-is.
No gate-resolution code path changes. §3 below states exactly what happens
to every existing page — nothing is deleted in Phase 1.

## 1. Every card type the Ledger renders

Each card type below is backed by a **real, currently-produced** row —
verified by reading `store.py`'s schema and every `log_event()`/
`create_gate()` call site, not invented. Where a real gap exists (a chain
this system doesn't yet fully instrument), it's named explicitly rather
than papered over, matching this codebase's own "Known gap" convention.

The Ledger's backing query is a **union across four existing tables** —
`events`, `gates`, `deliveries`, and the merged `llm_decisions` view — not
a new table. Rows share `assessment_id` and, where populated, `correlation_id`
(events only; gates/deliveries are joined to their chain via `assessment_id`).

| # | Card type | Real source | Data shown | Buttons | Uncertainty surfaced? |
|---|---|---|---|---|---|
| A | **Assessment complete** | `events` row, `action="assessment-complete"` / `"reassessment-complete"` | Score, delta vs. previous, criticality, dimension breakdown link | "Review Findings", "Onboard This App" | Split baseline-vs-LLM-inferred findings (see below) — no single confidence number exists for a score itself |
| B | **Fix / Onboarding generated** | `events`, `action="fix-generated"` / `"onboarding-complete"` | Category (Fix) or full plan (Onboard), agent/skill, file count | "Review & Deliver" → `onboard-results` | None — this is generation, not a decision; a card here correctly shows no confidence, since none was computed yet |
| C | **Classifier ruled (auto-mode decision)** | `events`, `action="decision"` | AUTO-APPLY or GATE, **the real confidence number and reason already embedded in the summary string** (`automode.py::should_auto_apply` literally formats `f"LLM classified as safe ({confidence:.2f}): {reason}"` / `f"LLM confidence too low ({confidence:.2f})"` / `f"LLM flagged as destructive: {reason}"`) | None if AUTO-APPLY (informational); implicitly followed by card D if GATE | **Yes — this is the card that structurally implements the Council-insight.** Render confidence as a labeled number against the real 0.80 threshold (`_CONFIDENCE_THRESHOLD`), and render the three outcomes (unavailable / below-threshold / flagged-destructive) as three visually distinct sub-states, never one generic "gated" label |
| D | **Gate opened** | `gates` row, `status="pending"` | `gate_type` (one of the 8 real types: `finding-{category}`, `auto-mode-review`, `dry-run-failed`, `gitops-pr-pending`, `auto-mode-scope-review`, `rollback-review`, `cluster-conflict-review`, `cluster-admin-review`), summary, `delivery_confirmation` ("AgentIT will: X — because Y") | **Exactly `gate_card`'s existing three buttons, unmodified**: Approve & Deliver / Reject / Dismiss | For `auto-mode-review`/`auto-mode-scope-review` gates, join back to the paired card-C decision event (same `target_app`, adjacent timestamp) and show its confidence number inline, not just the gate's own prose summary |
| E | **Gate resolved** | `gates` row, `status` ∈ (`approved`,`rejected`,`expired`,`cancelled`) | Outcome, `resolved_by`, rejection reason if any | None (historical) | N/A — resolution is a fact, not a probability |
| F | **Delivery routed** | `deliveries` row | `mechanism` decoded via the **exact same** `MECHANISM_DESCRIPTIONS` dict already used in the confirm modal — never a re-worded summary that could drift from what the user actually saw before approving | Link to the delivery's verification status once known | **Explicitly none** — mechanism selection is deterministic (GitOps-registered or not), not probabilistic; a card here must not manufacture a confidence number that doesn't exist |
| G | **Fix-review decision** | `llm_decisions` (`decision_type="fix-review"`, sourced from `skill_effectiveness`) | Skill name, approved/rejected, reason | None (historical) | Real per-skill attribution already exists here — surface it as-is. Note: this only appears when `self-fix --create-pr` (CLI) has been run; the Ledger correctly shows CLI-triggered activity exactly like portal-triggered activity, since the underlying tables don't distinguish trigger source and shouldn't need to |
| H | **Watcher tick** | `events`, `action="tick-complete"`/`"tick-failed"` | Watcher name, status | None | Lowest-salience card type — this is the primary target of the noise-reduction rules in §2 |
| I | **Critical findings / CVE detected** | `events`, `action="critical-findings-detected"` (vuln-watcher) or `"remediation-failed"` | Finding count, app, severity | Link to Findings tab; if it chains into an auto-remediation attempt, group with the resulting cards B/C/D by `target_app` + adjacent timestamp | N/A |
| J | **SLO breach / rollback recommended** | `events` (`rollback-recommended`, published to Kafka `TOPIC_ALERTS` — not currently also written to `events`, see gap note below) + the resulting `rollback-review` gate (cards D/E) | Breached metric(s), current vs. target value | Same gate buttons as D once the gate exists | N/A — SLO breach is measured, not classified |
| K | **Drift detected** | `events`, `action="drift-detected"` | Argo app, sync/health status | Link to Argo/Health | N/A |
| L | **API removed / skill auto-deprecated** | `events`, `action="api-removed"` (critical) + `"skill-deprecated"` | Removed API, affected skill(s) | None | N/A — this is the system explaining its own self-correction, a strong trust-building card as-is |
| M | **Skill learning run** | `events`, `action=LEARNING_RUN_ACTION` | Trigger (manual/watcher), what was researched, outcome (drafted / no-op / error) | Link to draft skill's Activate button on Capabilities | This flow **already previews "what it's about to do"** per the README's own description — the one place in the current codebase that already does exactly what this whole exploration has been asking for; the Ledger just gives it a permanent, chronological home instead of a transient toast |
| N | **Catalog change** | `events`, `action` ∈ (`skill-added`,`skill-removed`,`check-added`,`check-removed`,`skill-activated`) | What changed | Link to skill detail | N/A |
| O | **Self-improvement run (capability-scout)** | `events`, `action=CAPABILITY_RUN_ACTION` | Outcome (proposed/gate-blocked/error/no-signal), PR link if proposed | Link to PR | Same shape as M |
| P | **Manual trust-affecting setting change** | `events`, `action` ∈ (`auto-mode-toggled`,`auto-mode-allowlist-added`,`auto-mode-allowlist-removed`) | What changed, by whom | None | This is deliberately **never collapsed/low-salience** (§2) — toggling auto-mode has no gate of its own today, so this card is the *only* visible trust signal for that specific action, and must render with the same prominence as a gate, not buried as routine telemetry |

**Chain expansion.** Any card whose event row has a non-null `correlation_id`
renders a small "Part of a chain (N events) ▸" affordance that expands
in place to show every sibling row sharing that id, ordered by timestamp —
this replaces the current Events page's separate `?correlation_id=` link-
and-reload with an inline expand, using the exact same query
(`list_events(correlation_id=...)`) that page already runs.

**Two real, honest gaps this design surfaces** (not invented, not hidden):

1. `slo_tracker.py::_recommend_rollback()` publishes to Kafka's
   `TOPIC_ALERTS` but never calls `store.log_event()` — so today, an SLO
   breach is *only* visible via the resulting `rollback-review` gate (card
   D), not as its own card J. **Fix required for card J to exist as
   described**: add one `store.log_event()` call alongside the existing
   Kafka publish, mirroring the pattern every other watcher already uses.
2. `drift_detector.py::_maybe_auto_sync()` has no `log_event()` call on
   success or failure — only a `click.echo()`. **Fix required for "drift
   auto-synced" to be a real card**: add the same one-line pattern. Until
   then, an auto-sync attempt is invisible in the Ledger exactly as it's
   invisible everywhere else today — the spec doesn't pretend otherwise.

Both are one-line additions consistent with every other watcher's existing
`log_event()` convention — flagged here as Phase 1 prerequisites (§5), not
speculative future work.

## 2. The noise-at-scale answer

A 200-app fleet produces overwhelmingly watcher-tick and routine-delivery
volume. The concrete mechanism, in order of what actually reduces volume:

1. **Per-app view is the default entry point, fleet-wide is opt-in.**
   Arriving from an app's own Assessment Detail always shows that app's
   stream, pre-filtered to its `assessment_id`. The fleet-wide view is a
   separate, explicit navigation ("View all apps"), not the default landing
   surface — mirrors how `fleet.html` already works today (per-app detail
   is the primary unit; the fleet table is the index).
2. **Fleet-wide view groups by app, collapsed by default.** One row per
   app (repo name, current score, GitOps badge, pending-action count — all
   fields `fleet.html` already computes) with its single most-recent/most-
   severe card summarized inline; clicking a row expands that app's own
   filtered stream in place. This is the same shape a PR-review tool uses
   for "N commits, click to expand" — not a new interaction pattern.
3. **A "Needs You" filter, on by default in the fleet-wide view.** Shows
   only apps where: a pending gate exists (`pending_actions_count > 0`,
   already computed), OR a gate is stale (`admin_review.py`'s existing
   `get_stale_gates(hours=4)` computation, generalized fleet-wide), OR an
   unresolved SLO breach exists, OR a watcher's last tick failed within the
   configured interval (the same signal `AgentITWatcherStale` already
   alerts on via Prometheus). Toggling it off shows everything, including
   routine/healthy apps — "Needs You" is a filter, never the only view.
4. **Consecutive routine watcher ticks collapse into one summary row.**
   N consecutive `tick-complete` events from the same watcher within a
   rolling window (e.g., "vuln-watcher: 6 clean ticks since 08:00") render
   as one low-salience row; a single `tick-failed` breaks the collapse and
   renders as its own full-salience card (H is the one card type this rule
   targets — no other card type is ever collapsed this way, since every
   other type already represents a discrete, meaningful thing that
   happened).
5. **Search/filter reuses exactly the fields Events and Decisions already
   filter on** (`q` text, severity, decision_type, attribution, app) as one
   unified filter bar — no new filtering vocabulary invented for the
   Ledger specifically.

## 3. What happens to every existing page

| Page | Fate | Why |
|---|---|---|
| **Fleet** (`/`) | **Kept, evolves into the entry point.** Unchanged GitOps badge, pending-actions badge, deploy-status badge. Each row gains an inline expand into that app's Ledger stream (see §2.2) — additive, no existing column removed. | It's already the correct "per-app index" shape; the Ledger becomes what a row expands into, not a replacement for the index itself. |
| **Assessment Detail** — Overview tab | **Kept unchanged.** Dimension scores, score history table, GitOps badge/nudge. | A sorted dimension bar chart and a score-history table are structured, tabular data — the wrong shape for a chronological feed. |
| **Assessment Detail** — Findings tab | **Kept unchanged.** Per-finding Fix button, Remediation Plan table. | Findings are a point-in-time snapshot of *current* state ("what's wrong right now"), not a history of *events* — a feed answers "what happened," not "what's true right now"; both are needed, and conflating them would make Findings worse, not better. |
| **Assessment Detail** — Actions tab | **Absorbed.** Replaced by the app-scoped Ledger stream, filtered to cards D/E (gates) — same `gate_card` macro, same three buttons, just rendered as cards in the stream instead of a flat list. | This tab is *exactly* "pending gates for this app," which is already one filtered slice of the Ledger — no reason to maintain it as a separately-templated list once the Ledger exists. |
| **Assessment Detail** — Timeline tab | **Absorbed**, into the same app-scoped Ledger stream (unfiltered — all card types A–P for this `assessment_id`). | This tab is already "chronological events for this app" — literally the Ledger's exact shape, scoped to one app, already proven out in miniature. |
| **Admin Review** | **Kept unchanged, not touched.** | The one gate type that's genuinely cross-app for a genuinely different, elevated-RBAC audience. Folding it into a fleet-wide Ledger view would leak `cluster-admin-review` gates to app owners who structurally cannot act on them — actively the wrong move, not a missed absorption opportunity. Fleet-wide Ledger cards for this gate type render as a non-actionable pointer ("held for elevated review — see Admin Review"), never inline buttons. |
| **Capabilities** — Catalog/Registry tabs | **Kept unchanged.** Browsing all 40 skills, all agents/watchers, triggering "Research CVEs & Generate Skills"/"Run Self-Improvement Scan." | Browsing and activating a skill is a catalog-lookup task, not a chronological-narrative task — the wrong shape for a feed. |
| **Capabilities** — "Recent Catalog Changes" / "Learning Agent Runs" / Self-Improvement's run table | **Absorbed** as card types M, N, O. | These are already narrative, timestamped, "what happened and why" tables — exactly the Ledger's shape, just siloed per-tab today. |
| **Decisions** — per-row decision log | **Absorbed** as card types C and G, with reasoning inline. | This page's entire purpose (surface the LLM's actual reasoning next to the outcome it gated) is what card C/G *are*. |
| **Decisions** — "By Agent/Skill" summary table | **Kept as a specialized secondary view** (likely folded visually into Insights in Phase 3). | An approve/reject-rate rollup per skill is aggregate analytics, not a narrative — same "definitions/rollup table stays, narrative events become cards" split applied consistently. |
| **Events** — main table | **Retired as a distinct destination** (Phase 3, see §5) — the Ledger is a strict superset (every event Events shows, plus gates/deliveries/decisions Events never had). | No remaining job this page does that the Ledger doesn't do better. |
| **Events** — DLQ sub-tab | **Kept unchanged.** | "Retry this specific failed message" is an ops-recovery tool, not a narrative feed item — wrong shape for a card. |
| **Health** | **Kept unchanged.** | Pod/rollout/Kafka/circuit-breaker status is live infrastructure telemetry (current state), not a history of discrete things that happened — same category distinction as Findings above. |
| **SLOs** — definitions/error-budget table | **Kept unchanged.** | Structured definitions data, not a narrative. |
| **SLOs** — breach/rollback narrative | **Absorbed** as card type J. | Same split as Capabilities/Decisions. |
| **Insights** | **Kept unchanged.** | Fleet-wide aggregate analytics (approval rates, compliance %, loop health) — a fundamentally different data product than a chronological stream; a feed cannot replace a percentage. |
| **Settings** / **Schedules** | **Kept unchanged as the control surface** — you still need a page to flip a toggle, edit an allowlist pattern, or edit a cron schedule. | The Ledger records the *effect* of using these controls (card P); it isn't a substitute for the controls themselves. |

## 4. The rewind feature (Ghost Run's non-speculative half only)

**Explicitly not built:** the predictive "fast-forward and show what
probably happens next" half. Per this exploration's own earlier critique of
Ghost Run, that half requires confidence data this system doesn't actually
have for arbitrary future outcomes, and would risk fabricating a forecast —
directly against this project's own no-mock-data, never-fabricate ethos.
Only the **rewind** half ships.

**Concrete UI.** On any Ledger card that belongs to a `correlation_id`
chain, a "◀ Replay this chain" control opens a horizontal scrubber — a
timeline slider with one tick mark per card sharing that `correlation_id`,
ordered by timestamp. Dragging the position marker re-renders the main
panel to show exactly that one historical card, full detail, **read-only**
(no action buttons — a `gates` row that's already resolved has nothing left
to approve; this scrubber shows history, it doesn't let you re-decide it).

**Reuses, verbatim, data this system already computes for the exact same
purpose:**
- `events` rows `WHERE correlation_id = X` — the same query the current
  Events page's "Chain" column link already runs (`/events?correlation_id=X`).
- `gates` rows `WHERE assessment_id = <the id correlation_id resolves to>`.
- The `deliveries` row(s) for that `assessment_id`.
- `llm_decisions` rows filtered by `target_app` + the chain's time span.

This is, concretely, **the existing "Chain" link, reshaped into a scrubber
instead of a flat filtered table** — no new backend query, no new data
model, no speculative logic. It's the smallest possible version of "replay
a past incident step by step" that's honest about only ever showing what
actually happened.

## 5. Rollout sequencing

Each phase is independently shippable, independently testable, and leaves
a working product if the next phase is delayed or cancelled — matching
this project's own "ship incrementally, verify each step" discipline
already visible in how the Gates dissolution itself shipped.

**Phase 0 — prerequisite gap-fills (small, isolated, ship first).**
Add the two missing `log_event()` calls named in §1 (`slo_tracker.py`'s
rollback recommendation, `drift_detector.py`'s auto-sync attempt). Zero UI
change. Testable in isolation: assert the event row exists after each code
path runs. This is the one piece of "real work" this spec asks for before
any Ledger UI exists at all, because without it, card types J and part of K
would otherwise be under-built on day one.

**Phase 1 — Ledger as new, additive surfaces, nothing existing removed.**
- A new backing function (e.g. `ledger.py::get_ledger_cards(assessment_id=None, filters=...)`)
  that unions `events` + `gates` + `deliveries` + `llm_decisions` into the
  card shapes in §1 — read-only, no changes to any existing write path.
- A new **per-app Ledger tab** on Assessment Detail, added *alongside* the
  existing Actions and Timeline tabs (5 tabs total, temporarily), opt-in —
  a user can ignore it entirely and the app behaves exactly as today.
- A new **global `/ledger` page**, linked from nav as an *addition* (8th
  nav item, temporarily) — Events and Decisions stay exactly as they are.
- Testable independently: new routes, new templates, zero modification to
  `gate_card`, `resolve_gate()`, or `route_and_deliver()`. A regression in
  the Ledger cannot break gate resolution, because it doesn't touch that
  code.

**Phase 2 — promote the Ledger to be the default, retire duplicated tabs.**
- Assessment Detail's Actions + Timeline tabs collapse into the one Ledger
  tab (4 tabs → 3: Overview, Findings, Ledger).
- The global Ledger replaces Events' nav slot; `/events` becomes a
  redirect to `/ledger` (mirroring the exact `/gates → /admin-review`
  redirect pattern already used once in this codebase) — DLQ keeps its own
  reachable sub-page.
- Decisions' per-row log becomes a redirect into the Ledger filtered to
  decision cards; its aggregate summary table is preserved, relocated
  (likely into Insights, per §3).
- Testable independently: redirect status codes, and that every card type
  the retired tabs used to show still renders somewhere in the Ledger with
  equal or better information (a concrete acceptance check per card type
  in the §1 table).

**Phase 3 — delete dead templates/routes.**
Once telemetry shows near-zero real traffic to the Phase-2 redirects for a
defined window (this codebase already has precedent for exactly this
judgment call — the `/apply`/`/create-pr` route retirement in
`docs/unified-apply-flow.md`), delete `events.html`'s main-table code path
and `decisions.html`'s per-row log code path. The redirects themselves can
stay indefinitely for stale bookmarks, matching the existing `/gates`
pattern.

**Phase 4 — refinements, optional, layered on a validated base.**
The rewind scrubber (§4), the "Needs You" filter and tick-collapsing (§2),
and any fleet-scale performance work (pagination/virtualization for a
200-app union query) ship here — deliberately sequenced *after* the base
Ledger has real usage, not bundled into Phase 1's MVP, so none of Phase 1's
value is gated on the more speculative pieces of this spec.
