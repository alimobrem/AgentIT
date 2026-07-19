# Capabilities page UX redesign — design notes

**Status: implemented.** Short internal note (not a formal spec) covering the
decisions behind the Capabilities/Registry redesign, written before
implementation per the request that produced it. Answers the specific
product questions raised about the Capabilities page; see PR/commit history
for the resulting template/route changes.

## 1. Registry vs. Catalog — investigated, not assumed

Checked `docs/extension-model-unification-spec.md` (present on `main`) plus
`docs/extension-model-unification-plan.md` /
`docs/extension-model-unification-plan-phase2.md` (found only as **uncommitted
files in a different local worktree**, `AgentIT-ui-redesign`, dated
2026-07-17 — not on `main`, not on any branch, not referenced by any commit).

**Finding: none of that unification work is implemented, and none of it is
about Registry vs. Catalog.** All three docs are exclusively about a
different, backend-only redundancy: **checks vs. analyzers** (two systems
that independently score the same 7 assessment dimensions). Their own
explicit recommendation (spec §2, "Option B") is to keep **skills and checks
as two separate systems** — "conflating them adds risk for no proven
benefit." The agents-vs-skills question is also explicitly called
already-resolved (by `docs/agent-removal-readiness.md`, weeks earlier): skills
already cover every domain a Python agent used to own except `codechange`,
`cost`, `dependency` (kept for narrow, non-skill-shaped reasons). So "we made
agents and skills the same thing" is *mostly* true at the backend level
already — but that fact has nothing to do with the Catalog/Registry tabs.

**What Catalog vs. Registry actually are, verified by reading the routes and
templates directly:**

| Tab | Route | Shows |
|---|---|---|
| **Catalog** | `/capabilities` | Definitions: skills (by domain), checks (by dimension), onboarding agents, watchers — "what AgentIT can do" |
| **Registry** (renamed **Agent Activity**, see below) | `/agents` | Live telemetry: which agents/watchers have actually run, heartbeat freshness, success rate — "what's actually happened" |

These are **not duplicate data** — one is static/definitional, the other is
runtime/observed. Consolidating them into one table would conflate two
different truths (e.g. an onboarding agent has a reference row on Catalog
whether or not it's ever run; it only gets a Registry row after a real run).
**Decision: do not force-merge them.** The real problem is narrower and real
in a different way:

1. **The tab labels are needlessly ambiguous.** "Registry" and "Catalog" are
   near-synonyms out of context — neither word tells a first-time reader
   which one is "what could happen" vs. "what did happen." Renamed the
   **Registry** tab label to **Agent Activity** (kept the page's own H1,
   "Agent Registry," and its own already-correct one-line explanation
   unchanged — this is a tab-strip legibility fix, not a page rename).
2. **The two views weren't cross-linked**, even though they're clearly
   related (an onboarding agent's Catalog reference row and its Registry
   run-history row are the same real agent). Fixed by linking every
   onboarding-agent/watcher name in Catalog's reference tables to its real
   `/agents/{name}` detail page — zero new data, just closing an existing gap
   (`get_onboarding_agents()` already returns the real registry key as
   `category`; watcher dicts already use the real registry key as `name`).

This is a "leftover from organic growth," not from the (unimplemented,
unrelated) unification docs: the routes module's own docstring says Agents
"live alongside the skill catalog... routes here rather than in their own
module" — i.e. Agents was folded into Capabilities' navigation without a
matching information-architecture pass.

## 2. What should the user actually do here, and is it transparent?

Real jobs, in priority order: **(1)** understand what capabilities
(skills/checks/agents/watchers) exist and their health at a glance, **(2)**
review and act on the one thing that's actually pending — LLM-drafted skills
awaiting human activation — without hunting, **(3)** trust the system via
real transparency (catalog change history, learning-run history, per-skill
effectiveness trend, activation/deprecation history), **(4)** understand how
the onboarding pipeline works as reference material.

Job (2) was the concrete, measurable failure: **there are 3 real draft
skills on disk right now** (`skills/security/cve-2019-5736-*`,
`cve-2018-1002105-*`, `cve-2021-25741-*`, all `source: learning-agent`,
awaiting review) and the only way to act on them was to open a collapsed
"Skills by Domain" section and find them among 45 skills across ~14 domains.
Added a **"Needs Review"** section, uncollapsed, directly under the stat
grid — the same "surface what's actionable, keep static reference collapsed"
principle the page already applied inconsistently.

## 3. CRUD: add / delete / pause — checked real backend capability first

- **Add a skill:** already real (**Research Skills** → LLM drafts a skill).
  Kept as-is; promoted its button from secondary (`.btn-outline`, wrong per
  EDL §2 — it's the one primary "go" action on this tab) to primary
  (`.btn-action`, matching Fleet's Scan/Re-scan convention).
- **Delete a skill:** not built. Nothing else in this app hard-deletes
  definitional, git-tracked content from the portal (Fleet's Delete removes
  a *tracked app's runtime records* — a different class of object). Built
  the real, already-precedented alternative instead: **Deprecate** (active →
  deprecated, with a required reason, persisted via the same
  git-commit-and-open-PR flow `activate_skill_route` already uses) — this
  mirrors the drift-detector's existing *automatic* deprecation
  (removed-API-triggered) but makes it human-triggerable for any reason, and
  is reversible: extended `activate_skill_route` to also promote
  `deprecated → active` (verified via the same `verify_skill()` gate), not
  just `draft → active`, so deprecation isn't a dead end.
- **Pause/resume an agent:** deliberately **not built**. "Agents" here are
  either (a) stateless onboarding agents with no running process to pause,
  or (b) long-lived watchers whose on/off state is a Helm chart value
  (`agents.*.enabled`) applied via GitOps. A portal toggle that scaled a
  Deployment directly would be an ungoverned, non-git-tracked cluster
  mutation — the exact failure mode `_persist_skill_activation` (now
  `_persist_skill_status_change`) exists to avoid for skills, and directly
  against `capability_scout.py`'s own "never a direct commit/mutation
  outside git" convention this codebase already commits to. Not adding a
  button that can't actually keep its promise.
- **Edit a skill:** deliberately **not built**, matching the user's own
  instinct. Skills are LLM-generated Markdown+YAML matched by exact trigger
  keywords, and a draft only becomes safely activatable after
  `verify_skill()`'s functional generation smoke test. A free-text editor
  would either bypass that gate (silently ship an unverified edit to an
  active skill) or have to re-run the whole generate→verify→activate cycle
  anyway — at which point it's not "editing," it's the existing flow with
  extra steps. The real gap wasn't the lack of an editor; it was that the
  **review** step (before activate/deprecate) showed almost nothing about
  what a skill actually does. Fixed that instead (see §4).

## 4. Skill detail view — real content, real actions

`skill_detail.html` existed but only showed status/approval-rate/domain +
effectiveness trend + lifecycle history — none of *what the skill actually
does*. Added (all from fields the `Skill` dataclass already loads from disk,
no new data): mode (`template` vs `llm`), version, source, created_at,
triggers, outputs, `requires_crd`/`conflicts_with` (shown only when
non-empty), the property description, and the skill's raw Markdown body
(property/constraints/verification sections + template block) in a
collapsed `.code-block`, plus the real Activate/Reactivate/Deprecate actions
(previously only reachable, for Activate, from the collapsed list row).

## Explicitly not done (scope discipline)

- Did not touch the checks-vs-analyzers unification work — unrelated to this
  page, still an unimplemented (and, per its own §2, deliberately not
  Option-A) plan; not this task's concern.
- Did not rename the `/agents` route, its H1, or its data model — only the
  tab-strip label, to fix legibility without breaking any bookmarked URL or
  existing "Agent Registry" page-content assertion.
