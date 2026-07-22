# Next-Gen UX Concepts — a blue-sky brainstorm

**Status: strategic brainstorm, not a proposal to build as specified.** Unlike
`docs/ui-redesign-proposal.md` (an incremental IA fix for the *current*
multi-page dashboard), this doc is explicitly a from-scratch exploration of
fundamentally different interaction models for AgentIT's whole product
experience. Nothing here is scoped, sequenced into tickets, or ready to
implement as-is — it's raw material for a product-direction decision. See
the companion chat response this doc was written alongside for the framing,
comparative analysis, and recommendation in full prose; this file preserves
the five concepts themselves for future reference.

## Why this exists (the brief)

> "I want you to brainstorm a whole new user experience. Start from scratch
> based on what our goals are and the customers we're trying to help...
> Our goal is to take the burden of getting an app into prod, and making it
> future-proof. Since this will be AI-driven, trust and transparency are a
> must at every step."

The current portal (Fleet → Assessment Detail → Gates/Actions →
Capabilities → Decisions → Events → Health/SLOs/Settings) is a traditional
multi-page dashboard that a prior review found "feels disconnected" — see
`docs/ui-redesign-proposal.md` for the concrete, page-by-page audit of why.
That doc is an incremental fix to the existing IA. This doc asks a different
question: if the multi-page-dashboard *shape itself* weren't a given, what
would a sysadmin/platform-engineer experience for an AI-driven prod-hardening
tool actually look like, across genuinely different interaction models?

The hardest constraint, verbatim from a persona playing the real target
user: *"I would not flip `AGENTIT_AUTO_MODE=1` on anything I'd lose sleep
over"* without seeing, at every consequential step, a clear **"AgentIT is
about to: X, because Y"** statement — not automation happening invisibly.
Every concept below is evaluated against that bar specifically, using
AgentIT's real safety-gate and delivery-routing mechanics (the LLM safety
classifier's ≥0.8 confidence threshold, the 8 real gate types, the
`route_and_deliver()` direct-apply/GitOps-PR/source-PR/admin-review taxonomy,
skill effectiveness tracking, and the per-(namespace, kind) auto-mode
allowlist) — not hand-waved trust language.

## The five concepts

### 1. Foreman — conversational, chat-first

A persistent chat pane is the primary surface. Every AgentIT action —
assess, generate a fix, request a gate approval, report a watcher finding —
is a conversational turn that states intent and reason *before* acting,
in the same modality the user is already reading, and takes a plain-language
reply as the decision. "AgentIT is about to: X, because Y" isn't a banner
competing for attention; it's the literal sentence shape of every turn.

Strong for synchronous, attended decisions (gate approvals, fix reviews);
structurally can't help with truly unattended AutoMode firings — the best
it can do there is a proactive push message after the fact, which is a
feed item wearing a chat costume.

### 2. The Ledger — one chronological feed, not pages

Every event — assessment run, skill match, LLM decision, gate opened, gate
resolved, delivery, watcher tick, rollback — is a card in one
reverse-chronological stream per app (and one fleet-wide). This is close to
a direct promotion of AgentIT's real `events` table + `correlation_id` chain
concept (today a buried "Chain" link on the Events page) to *the* primary
UI, instead of a filtered subsidiary view.

Most structurally transparency-forcing of the five: there's no separate page
to omit something from. If it happened — attended or not — it's a card, with
the same reasoning `llm_decisions.py` already tracks rendered inline.

### 3. The Bridge — mission control for a fleet

A dense wall of per-app tiles (score sparkline, trust/risk glyph, pending-gate
count, sync status), built for scanning 50–200 apps at a glance rather than
reading any one deeply. Weak as a first-run single-app onboarding surface;
strong as the "staying future-proof at fleet scale" answer. Glanceability is
double-edged — density is the whole point, which means it's also the easiest
of the five to reduce "why" down to a bare color dot unless every glyph is
disciplined to be one click from its literal reasoning sentence.

### 4. Git-native — no separate portal

Meet engineers where they work: `agentit assess .` from the CLI (already
real), assessment/drift/CVE findings as PR comments or GitHub Issues on the
app's own repo, delivery review as an ordinary PR review (already exactly
how the GitOps-registered and source-patch delivery paths work today — real,
not invented). Strongest for the half of delivery that's already PR-shaped.
Has a genuine structural blind spot: the direct-apply path (unregistered
apps) has no PR at all today, so a purist git-native experience has nothing
native to attach a live "about to touch your cluster" moment to for exactly
the riskiest case, without bolting on a non-git approval surface anyway.

### 5. Autopilot / Trust Ladder — earn autonomy, don't toggle it

The primary object per app isn't a score or a page — it's a computed trust
tier (Observed → Supervised → Trusted → Autonomous) built from real,
existing signals: recency-weighted skill effectiveness, this app's own
classifier-confidence history, clean-apply/no-rollback streaks, and the
per-(namespace, kind) allowlist that already exists. The UI's whole job is
showing the evidence for why a tier is what it is, and demoting it
automatically as confidence drops — never presenting the single scary
global `AGENTIT_AUTO_MODE` boolean the reviewer specifically distrusted.
Answers "should I trust this app" precisely; says nothing on its own about
"how do I clear today's 12 pending approvals" — it needs to borrow the
Ledger or Foreman as its actual gate-resolution surface.

## Comparative snapshot

| Axis | Foreman (chat) | Ledger (feed) | Bridge (mission control) | Git-native | Autopilot (trust ladder) |
|---|---|---|---|---|---|
| Trust-building mode | Synchronous, per-decision | Continuous, every action logged live | Glance + drill-in | PR review norms (half the paths only) | Pre-hoc: evidence for *why* to trust |
| Best fleet size | Small (1–10 apps) | Any size | Large (50+) | Any, dev-centric | Any |
| Onboarding curve | Lowest (just talk) | Low | Highest (needs volume to be useful) | Low for CLI-fluent users, opaque otherwise | Medium (new mental model: tiers, not toggles) |
| Structurally forces transparency? | Mostly (weak for unattended) | Strongest | Weakest (density invites under-explaining) | Strong for PR paths, blind spot for direct-apply | Strong, but only for the trust decision, not day-to-day gates |
| Feasibility on Jinja2+htmx+Alpine today | Medium (needs a turn/intent layer) | High (reuses existing events + correlation_id model) | Medium (bulk live updates want SSE/websockets at real scale) | High on CLI/PR side; new integration surface for direct-apply gates | High (mostly backend/query + existing templates) |

Full reasoning behind every cell, the "why" narrative, and the actual
build-first recommendation live in the chat response this doc accompanies —
intentionally not duplicated here in full to avoid this doc drifting out of
sync with that reasoning.

## Recommendation (summary)

Build **The Ledger** first — it's the cheapest of the five on the current
stack (no SPA/websocket layer required, reuses data that already exists) and
it's the one interaction model that structurally can't hide an unattended
action. Layer the **Foreman** pattern's literal "about to: X, because Y"
sentence discipline onto every card and gate the Ledger renders (cheap,
no new interaction model — just a templating/copy change). Build
**Autopilot**'s tier model next as the evolution of the Settings/allowlist
UI once the Ledger exists to supply its evidence trail. Sequence **The
Bridge** third, once enough apps/feeds exist to make a fleet-wide rollup
worth building. Pursue **Git-native** as a parallel, incremental CLI/PR-bot
investment (most of it is already real) rather than a portal replacement —
it cannot alone close the direct-apply trust gap that's the hardest part of
this brief.
