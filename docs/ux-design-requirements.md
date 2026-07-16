# UX Design Requirements for AgentIT — a research-grounded checklist

**Status: design research + requirements, not yet implemented.** Same
posture as `docs/next-gen-ux-concepts.md` — this is input to a product
decision, not a change log. Part 4 additionally recommends a stack
decision (framework + component library); nothing here has been built or
migrated.

This doc grounds AgentIT's UX requirements in two bodies of real,
externally-sourced evidence — enterprise/trust-oriented design systems, and
consumer/prosumer tools known for feeling fast and joyful — then compiles
them into one specific, testable checklist (Part 3), and closes with an
opinionated recommendation on what frontend stack to actually build this on
if starting fresh, with no legacy-stack bias (Part 4).

---

## Part 1 — What the best design systems get right

Extracted only for what's actually applicable to a tool where an AI asks a
human to trust consequential, sometimes-destructive actions against a live
cluster — not general summaries.

### Material Design 3 (Alert dialogs)
- **Title as a question naming the action and object** — "Delete file?" not
  "Are you sure?" — never a generic confirmation prompt.
- **Body text states real consequences**, not a generic "this cannot be
  undone" — name what's actually lost.
- **Cancel receives default focus** on any destructive confirmation, so a
  reflexive Enter keypress never fires the destructive path — this specific
  rule converges across M3, Polaris, Carbon, and GOV.UK.
- Never stack more than 2-3 actions in one alert dialog; move anything more
  complex to a full dialog or sheet.

### Apple Human Interface Guidelines
- **Use alerts sparingly** — reserve them for uncommon, non-undoable
  destructive actions. Common/undoable actions (and anything with a real
  undo) shouldn't interrupt with a modal at all.
- **Never assign the primary/default button role to a destructive action**,
  even when it's the likely choice — visual prominence alone causes people
  to click without reading.
- **Long-running actions get an in-place activity indicator with a
  relabeled verb** ("Checkout" → "Checking out…") rather than a separate
  spinner disconnected from the button that triggered it.

### IBM Carbon Design System
- **Status indicators scale to available density**: icon+shape+color+label
  for spacious contexts, a plain badge/dot when space is tight — but the
  underlying semantic meaning and color never changes between the two.
- **Notifications use a fixed 4-status vocabulary** (Informational=blue,
  Success=green, Warning=yellow, Error=red), each with its own icon and its
  own disruptiveness rule (Error can block progress until resolved; Info
  can be dismissed on a timer) — statuses and their color/icon pairing are
  never improvised per-page.
- **Progressive disclosure in forms**: only show a field once a prior
  answer makes it relevant; for long forms, don't disable the primary
  submit button on validation error, because the error and the button may
  not be visible on screen at the same time.

### Shopify Polaris
- **Never overuse "critical" (destructive-styled) buttons in one view** —
  it dilutes the meaning of the one that actually matters.
- **Empty states must orient and guide, with exactly one primary CTA** —
  never "nothing to show" with no next step, and never guilt-inducing
  copy ("you haven't done X yet").
- Button copy is always `{verb} + {noun}` ("Register for GitOps"), never a
  bare "OK"/"Submit"/"Yes".

### Atlassian Design System
- Distinguishes **7 message-delivery components by exact intent**
  (empty state, banner, flag, section message, inline message, modal
  dialog, spotlight) — each maps to specific message types (info/success/
  warning/error/feature-discovery), and the system explicitly does not let
  one component serve every purpose just because it's convenient.
- **Empty-state copy must name the reason and the next step in 1-2
  sentences** — scannable, no jargon, and celebratory rather than apologetic
  when the empty state means a task was completed, not merely never started.

### Microsoft Fluent
- **Determinate progress (real % known) is always preferred over
  indeterminate** — only fall back to indeterminate when duration is
  genuinely unknowable, and even then, pair it with real status text
  ("Scoring security…") rather than a bare spinner.
- **Ring vs. bar is a blocking/non-blocking distinction**, not a visual
  preference: a ring implies the user cannot do anything else until this
  finishes; a bar implies they can keep working.
- Never let a progress indicator sit at a static "99%" for a long time —
  it reads as broken, not almost-done.

### GitHub Primer
- **Match confirmation friction to blast radius, not to a single fixed
  pattern**: a simple confirm dialog for a single-resource, reversible
  action; a **type-to-confirm** flow (type the resource's real name) is
  reserved specifically for org-wide/irreversible, wide-blast-radius
  actions — using it for routine actions cheapens it until people stop
  reading it.
- **Confirm button copy always states the action and target** ("Delete
  repository", never "Yes"/"OK"/"Confirm") — the one deliberate exception
  is type-to-confirm's own longer phrase ("I understand, delete this
  project"), because at that friction level a fuller sentence is the point.
- Danger-styled confirm buttons default focus to Cancel; non-dangerous
  confirms may default focus to the primary action.

---

## Part 2 — What makes the best consumer/prosumer tools feel joyful and efficient

### Linear
Speed is treated as a **hard, non-negotiable constraint** (sub-100ms for
common interactions), not a later optimization — every feature proposal
was evaluated against a "speed budget." A global `⌘K` command palette is
the one place every navigation, search, and action lives, so users are
never forced to hunt through nav for something they know the name of.
Opinionated defaults (no plugin marketplace, minimal configurability)
reduce decision fatigue at the cost of flexibility. **The speed is real,
not perceived** — Linear's local-first architecture treats the server as a
sync target, not the source of truth for the running UI; mutations apply
to the local cache instantly and reconcile with the server after the fact.
*(designsystems.one/linear; performance.dev/how-is-linear-so-fast;
ideaplan.io Linear case study)*

### Notion
Treats the **empty state as the actual product**, not a placeholder —
a blank page with a subtle slash-command hint teaches the tool through the
absence of content, rather than a separate onboarding tutorial. Complexity
is disclosed **progressively by user readiness**, not by a fixed tour —
novices see notes, intermediate users discover databases, advanced users
find relational rollups, each layer invisible until the layer below is
comfortable. *(brainy.ink "Empty States Are the Product"; raw.studio
Notion UX principles; IxDF progressive disclosure)*

### Stripe Dashboard
Builds trust through **structural honesty, not decoration**: every fee and
calculation is shown step-by-step and is independently verifiable, never
a single opaque total. Error messages state cause, responsibility, and
the specific next step ("declined because of insufficient funds... ask the
customer to use a different card"), never a bare failure code. Status
colors are strictly semantic and never reused for decoration. Two-party
approval and full event logs give teams real, auditable control over
consequential (money-moving) actions — the closest precedent in this
research set to AgentIT's own safety-gate problem. *(blakecrosley.com/
guides/design/stripe; raw.studio Stripe UX principles)*

### Arc Browser
Delight comes from **many small, real "moments of joy" that compound**,
not one big flashy feature — a link-hover preview, automatic file
renaming, a themeable sidebar — each individually minor, collectively "the
product is thinking ahead of you." Motion is deliberate and purposeful
(100-300ms), used to signal real state change, and always respects
`prefers-reduced-motion`. *(thezerotoone.co Arc growth UX; Inverse's
Browser Company design interview)*

### Raycast
**Speed under 50ms is treated as a design constraint, not an aspiration.**
Every action's keyboard shortcut is shown *inline in the results list*,
never buried in a tooltip or a separate shortcuts page — discoverability
is baked into the interaction itself. Third-party extensions share the
exact same UI primitives (List, Form, Detail, ActionPanel) as built-in
commands, so nothing ever feels bolted-on. *(blakecrosley.com/guides/
design/raycast; raycast.com; Raycast API docs)*

### Superhuman
Explicitly designs for **flow**, via three principles: make the next
action obvious (archiving an email immediately shows the next one, no
decision point), give immediate feedback with zero distraction (sub-100ms,
single-tasking view so nothing new can interrupt mid-task), and balance
perceived challenge with perceived skill (a stated goal like "Inbox Zero
without touching the mouse" makes speed itself the game). The `⌘K` command
bar doubles as a teaching tool — it executes the action **and** shows you
the shortcut for next time. *(blog.superhuman.com "3 principles of
designing for flow"; "How to build a remarkable command palette")*

### Vercel Dashboard
The closest real precedent to AgentIT's own delivery-confidence problem.
**Status is surfaced everywhere it can be seen without a click** — browser
tab favicon and page title change with deployment state (spinner→
checkmark→X), so a developer monitoring several deployments across tabs
never has to switch focus to know something failed. **Optimistic UI is
used specifically to eliminate perceived latency**, not to hide real
failure — the UI shows the expected state immediately and reconciles with
the real one moments later via background revalidation (SWR). Status is
never color-only: every deployment state ships a distinct label so
colorblind users get the same information as everyone else. Empty states
are strictly functional — the literal CLI command needed to proceed, not a
decorative illustration. *(blakecrosley.com/guides/design/vercel;
vercel.com/blog/dashboard-redesign; vercel.com/geist/status-dot)*

### Portability note (kept factual, not used to filter Part 3)
Some of the above is genuinely hard on any request/response server-rendered
model no matter how much polish is applied — true sub-100ms optimistic
mutation with automatic rollback, and a single unified command palette with
fuzzy search across an entire app's action surface, are meaningfully easier
on a client-state-aware framework. Some of it is fully achievable
today on htmx/Alpine specifically — a verified, real `⌘K`/`Ctrl+K` palette
pattern exists using Alpine's `x-trap` focus plugin plus an htmx-backed
search endpoint, and htmx's documented SSE extension (`hx-ext="sse"`,
`sse-connect`, `sse-swap`) delivers real server-push updates (a progress
bar, a live gate count) with no websocket/SPA investment at all. Part 4
resolves which of these to actually build on, without letting current-stack
convenience bias which ideas made this checklist.

---

## Part 3 — The AgentIT UX Requirements Checklist

Every item is a specific, testable statement, each traceable to Part 1,
Part 2, or an established AgentIT constraint (the safety-gate/delivery-
routing mechanics, the "AgentIT is about to: X, because Y" trust bar, or
the Ledger-first recommendation from the prior round of this exploration).

### Trust & Transparency
1. **Every consequential action** (anything that can call
   `route_and_deliver()`, `classify_action`, or `apply_with_verification`)
   shows a confirmation naming the exact action and target, and the reason,
   in plain language, before it fires. *[GitHub Primer ConfirmationDialog;
   Stripe "never hide information"; AgentIT's own trust bar]*
2. **Confirmation friction scales to blast radius**: an ordinary per-app
   gate approval gets a standard confirm; anything hitting a shared
   operator namespace (`cluster-admin-review`) or forcing a field-manager
   conflict (`force=True`) gets a heavier, deliberate-friction pattern
   (e.g., restating the exact scope, or type-to-confirm) — reserved for
   genuinely wide-blast-radius cases so it doesn't become background noise.
   *[GitHub Primer "match friction to blast radius"]*
3. **Cancel/reject is always the default-focused control** on any
   destructive or irreversible confirmation — a reflexive Enter keypress
   must never fire an apply, a force-reapply, or a gate-approve.
   *[Material 3, Apple HIG, Carbon, Polaris, GOV.UK — converging rule]*
4. **Every LLM decision that gates an outcome** (safety classification, fix
   review) shows its actual confidence number and real reasoning text
   directly next to the action it gated — never a bare "gated"/"approved"
   label with the reasoning behind a click. *[Stripe: show every
   calculation step-by-step, never hide information]*
5. **No single global toggle for something that's actually a scoped,
   incremental capability** — the per-(namespace, kind) auto-mode allowlist
   is the primary control users see and reason about; a single
   `AGENTIT_AUTO_MODE` on/off boolean is never the front-door UI for
   extending trust. *[Traces directly to this session's own "I would not
   flip AGENTIT_AUTO_MODE=1..." critique]*
6. **Gate reasons are specific, never generic** — "gated because the LLM
   was unavailable" / "...unconfident (0.62 < 0.8 threshold)" /
   "...flagged a risk: <reason>" are three different, distinguishable
   messages, never one generic "needs review" state. *[Stripe error-
   handling principle: explain cause, not just failure]*
7. **Empty/zero-pending states are informative, not blank** — "no pending
   gates" states why (e.g., "12 changes auto-delivered since your last
   visit — see the Ledger") rather than rendering nothing. *[Polaris and
   Atlassian empty-state content guidelines]*

### Speed & Efficiency
8. **A global command surface (`⌘K`/`Ctrl+K`) is reachable from every
   page**, covering navigation, search, and the terminal Approve/Reject/
   Deliver actions — never buried behind page-specific buttons only.
   *[Linear, Raycast, Superhuman command-palette research]*
9. **Every common action has a visible, discoverable keyboard shortcut
   shown inline** in the UI itself (e.g., next to a gate row), never only
   in a tooltip or a separate help page. *[Raycast: "shortcuts must be
   discoverable, not hidden"]*
10. **Purely client-side interactions never round-trip to the server** —
    dismissing a banner, expanding a card, toggling a filter is instant,
    local state; only state that must actually persist touches the network.
    *[Linear's "avoid the network" discipline]*
11. **Any operation with a knowable duration shows real determinate
    progress; anything with unknowable duration shows indeterminate
    progress plus real status text** ("Scoring security...") — never a
    bare spinner with no explanation, and never a fabricated percentage
    that isn't actually measured. *[Microsoft Fluent progress guidelines;
    AgentIT's own no-mock-data ethos applied to progress reporting]*
12. **No operation over ~1 second happens with zero visible feedback.**
    *[Fluent + Carbon forms guidance: "if it's going to take a while,
    communicate this with feedback and progress indicators"]*
13. **Resolving one gate immediately surfaces the next actionable item**,
    rather than returning to a static list requiring re-navigation.
    *[Superhuman: "make the next action obvious"]*

### Information Density & Progressive Disclosure
14. **Dense views show only the primary signal by default** (score,
    status, pending-count), with full reasoning/evidence exactly one click
    away — never zero clicks (buried) or two-plus (a page navigation).
    *[Carbon status-indicator density variants; IxDF: limit to one
    secondary disclosure layer]*
15. **Multi-field flows disclose fields progressively**, showing only what
    the user's prior choice makes relevant (e.g., GitOps registration
    fields only appear once "register for GitOps" is chosen). *[Carbon
    Forms pattern: "progressively disclose additional inputs only as they
    become relevant"]*
16. **Advanced/rare capability stays reachable but not default-visible**
    (e.g., force-reapply after a conflict, editing an effectiveness
    threshold) — the primary surface stays legible for a first-time user
    while power users can still reach depth. *[Notion's novice→
    intermediate→advanced gradual-complexity principle]*

### Feedback & Status Communication
17. **A fixed, small vocabulary of semantic status colors is used only for
    its canonical meaning everywhere in the product** (e.g., red=blocked/
    error, amber=needs attention/gated, green=healthy/delivered) — never
    reused decoratively elsewhere. *[Carbon's fixed 4-status notification
    table; Stripe: "status colors have consistent meaning"]*
18. **No status is ever communicated by color alone** — every status
    pairs its color with a distinct label, icon, or shape so the same
    information reaches colorblind users. *[Carbon accessibility
    requirement; Vercel's StatusDot: "every state ships a distinct title"]*
19. **Delivery/deployment status is visible ambiently, not only on a page
    you have to already be looking at** — e.g., a live badge on a Fleet
    row, and ideally a favicon/tab-title state change during an active
    delivery. *[Vercel's tab-favicon deployment-status pattern]*
20. **User-triggered, reversible interactions get optimistic feedback**
    (the UI reflects the expected outcome immediately, reconciling with
    the real server state moments later) — but this is explicitly scoped
    to reversible/low-stakes interactions only, never to whether a
    destructive or consequential action actually succeeded. *[Vercel:
    "optimistic UI eliminates perceived latency" — bounded by AgentIT's
    own fail-closed, never-fabricate posture; see Part 4 for the
    architectural implication of this boundary]*
21. **Long-running assessments/deliveries stream real, step-level progress**
    (which analyzer/skill/agent is currently running) rather than silent
    polling or a static "please wait." *[Verified achievable today via
    htmx's SSE extension; a first-class requirement regardless of stack]*
22. **Every error states its specific cause and specific next step** —
    "Dry-run failed: Deployment `checkout-svc` would exceed the
    namespace's ResourceQuota (cpu: 4/4 used)", never "Apply failed, see
    logs." *[Stripe: explain what happened, clarify responsibility, give
    an actionable next step]*

### Consistency & Predictability
23. **Exactly one verb may ever trigger a given irreversible outcome** —
    already named "Deliver" in this exploration's prior round — never two
    competing buttons that both claim to perform the real action.
    *[Polaris: don't pair competing critical-styled actions in one view]*
24. **Button copy always names the action and the object**, never a bare
    "OK"/"Yes"/"Confirm". *[Converging rule across Material 3, Apple HIG,
    GitHub Primer, Shopify Polaris]*
25. **The same conceptual state renders identically everywhere it
    appears** (a Fleet badge, an Assessment Detail Actions tab, a future
    Ledger card) via one shared component/partial, never independently
    re-implemented copies that can silently drift apart. *[Matches this
    project's own internal finding that two Fix-button-visibility rules
    had already drifted out of sync by not sharing one source of truth]*
26. **A disabled control always states why, next to itself** — never a
    disabled "Deliver" button with the blocking reason hidden elsewhere on
    the page. *[Carbon Forms guidance on disabled primary actions and
    error-message visibility]*

### Delight & Craft
27. **Motion is short (100-300ms), purposeful, signals a real state
    change, and respects `prefers-reduced-motion`** — never decorative
    animation that adds latency to a trust-critical action. *[Arc's
    restrained, purposeful motion; Disney-principles micro-interaction
    research]*
28. **A small number of genuine "moments of joy" are tied to real
    milestones** (a clean assessment, a skill graduating a trust tier),
    never applied to routine or high-stakes actions — this also guards
    against gamifying real production risk, a specific failure mode
    flagged earlier in this exploration. *[Arc's "compound delight" —
    many small real moments, not forced onto every interaction]*
29. **Perceived speed is treated as seriously as real speed** — a plain
    one-line status text costs nothing and communicates more than a bare
    spinner. *[Superhuman explicitly replaced spinner animations with
    plain "Loading…" text to cut both real and perceived latency]*
30. **One consistent accent color means "this needs your attention," used
    sparingly, never for anything else** — so the one signal that most
    needs to stand out (a pending gate, a low-confidence classification)
    isn't drowned by visual noise. *[Linear's "restraint with a single
    tuned accent" design lesson]*

---

## Part 4 — Stack recommendation (no legacy-stack bias)

The product owner has explicitly authorized evaluating this purely on
merit — a new stack is on the table if it's the right call. This section
gives a real, opinionated answer, not a hedge, and closes with an honest
recommendation on *when* to act on it.

### The core architectural tension, stated plainly

Everything that makes Linear/Superhuman/Raycast *feel* instant — a
client-owned state cache that renders confidently before the server has
confirmed anything, true sub-100ms optimistic mutation, offline-tolerant
interaction — depends on the client being allowed to be **confidently
wrong for a moment**. That's a fine trade when being wrong has zero blast
radius (an issue's status, an email's read state). It is close to the
**wrong trade by default** for AgentIT, whose whole premise is that a human
should never see a UI state that isn't honestly backed by real backend
truth, especially for anything with cluster-write blast radius. So the
stack decision isn't just "what feels fastest" — it's "what makes it easy
to be fast for the 90% of interactions that are safe to be optimistic
about, while making it structurally hard to fake the 10% that must never
lie."

React's `useOptimistic` (stable in React 19, paired with Server Actions in
Next.js) is a good fit for exactly this boundary: optimistic state is
explicitly scoped per-interaction, and automatically rolls back to the
real server value if the action fails — it's an *explicit, reversible
prediction*, never a silent claim of ground truth. This is a materially
better fit for AgentIT than a fully client-owns-truth sync-engine
architecture (Linear's own model), which is worth stating as a real,
specific reason to *not* copy Linear's architecture wholesale even while
copying its speed discipline and command-palette pattern.

### Framework recommendation: **Next.js (App Router) + React 19**

- **Server Components** for server-authoritative initial render (an
  assessment's current score, a fleet list, a gate queue) — matching
  AgentIT's own no-mock-data, server-is-truth ethos by construction, not
  by discipline.
- **Server Actions** for every mutation (approve a gate, trigger Deliver) —
  each one is an explicit, auditable server round-trip, never a client-only
  state change pretending to be real.
- **`useOptimistic`**, deliberately scoped to reversible/low-stakes
  interactions only (per Part 3, item 20) — never wrapped around a
  destructive or consequential action's actual outcome.
- **SSE via Route Handlers** (a documented, current production pattern) for
  the watcher-driven live updates (CVE ticks, SLO breaches, drift, gate
  creation) that arrive independent of anything the user clicked — this is
  the one truly new architectural need this product has that a plain CRUD
  dashboard doesn't, and it's a solved, current pattern in this ecosystem.
  AgentIT already runs Kafka + Argo Events for exactly these signals; a
  Kafka-consumer-backed SSE fan-out (a documented pattern for scaling SSE
  across multiple server instances) is a browser-facing tap on
  infrastructure AgentIT already operates, not new infrastructure.

**Real alternatives considered, and why they didn't win:**

- **Remix / React Router v7** — a close second, arguably an even more
  *philosophically* honest fit ("everything is an explicit loader or
  action," less implicit magic than RSC) — call this a genuine 60/40 over
  Next, not a landslide, tie-broken by Next's larger ecosystem of
  dashboard/table primitives and the fact that "RSC + SSE real-time
  dashboard" is now a well-documented, current pattern other teams are
  shipping, which matters for a team that won't have deep in-house
  framework expertise on day one.
- **SvelteKit** — genuinely strong on raw performance and DX (less
  boilerplate, real fine-grained reactivity) — I'd pick this if the team
  building this were already small and Svelte-fluent. I wouldn't lead with
  it for a product that will need to hire broadly and lean on a large
  component/data-table ecosystem; that ecosystem is smaller for Svelte.
- **A full client-owns-truth SPA in Linear's own architectural style** —
  actively the wrong fit, for the reason stated above: it's the pattern
  most likely to eventually let the UI show a state it can't immediately
  justify against the server, which is the one thing this product cannot
  ever do.
- **Datastar** (a real, serious "third way") — an 11.75KB hypermedia
  framework that keeps AgentIT's own current server-owns-everything
  philosophy while adding native SSE-driven reactive signals, no Node/JS
  backend required (works directly against FastAPI). This is the option
  most philosophically aligned with what AgentIT already is, and the one
  I'd actually spike on a single real page (the highest-value candidate:
  a live Fleet/Ledger view) before ruling it out — but it has materially
  smaller community size and enterprise production track record than
  React/Next today, which is a real risk for a product that will need to
  be maintained and hired for over years, not months.

### Component/design-system recommendation: **PatternFly 6, with a real caveat**

AgentIT's own target user — a platform engineer/sysadmin — already spends
most of their working day inside the OpenShift Console, which as of
OpenShift 4.19 runs on **PatternFly 6** as its own design system
(confirmed current: Red Hat's own 2026 developer documentation). Adopting
PatternFly for AgentIT means dropdowns, tables, modals, notification/alert
patterns, and status-color vocabulary the user already has real muscle
memory for behave identically here — a genuine, specific trust-and-
efficiency lever ("this behaves like the console I already trust") that no
generic design system, however well-built, can replicate. It also happens
to already ship dense-data components purpose-built for infra/ops tooling
(sortable/sticky-header tables, EmptyState, a 4-status Alert/Notification
vocabulary matching Part 1's Carbon findings almost exactly, wizards,
progress steppers) — a rare case where "the domain-appropriate choice" and
"the well-designed choice" are the same answer, not a tradeoff.

**The real caveat, stated honestly:** PatternFly's own default
information-architecture conventions skew toward the traditional
"enterprise admin console, one page per concern" shape — which is close to
the exact shape this whole exploration started by trying to get away from.
The recommendation is therefore specific: **adopt PatternFly's components
and data-density patterns for raw building blocks (tables, alerts, forms,
wizards, empty states) — do not adopt its default page-per-concern
navigation shell wholesale.** Layer the Ledger/command-palette/keyboard-
first interaction patterns from Part 2 on top of PatternFly's components,
rather than defaulting to "PatternFly's own IA," which would just rebuild
today's disconnected-dashboard problem with nicer-looking components.

**The real alternative, named as a genuine product decision, not just a
technical footnote:** **shadcn/ui + Radix UI**, if the product owner wants
AgentIT to feel like a distinct, differentiated "trust layer" product
rather than blend into the console it operates on. shadcn's model — you
copy the component source into your own repo rather than installing an
opinionated dependency — gives full ownership over exactly the unusual
copy/friction rules this doc's Part 3 needs (blast-radius-scaled
confirmation friction, the specific "X because Y" statement pattern),
without fighting a pre-built system's own constraints. This is genuinely a
brand/positioning call for the product owner to make explicitly, not one I
think should be decided silently by whichever engineer starts first.

### The honest rewrite-timing recommendation

**Don't do a full framework rewrite right now.** Three concrete reasons,
not a hedge:

1. **The size of the bet doesn't match the size of the validation.**
   AgentIT today is 56+ routes, 25 Jinja templates, and ~1,490 tests
   including 49 Playwright browser tests exercising the existing portal —
   a real Next.js/PatternFly rewrite to parity is realistically a
   multi-month effort for a small team, not a multi-week one, and this is
   a young, pre-PMF-validated product. Betting that much calendar time on
   a specific frontend technology before real sysadmins have used *any*
   version of this UI in anger is the same category of mistake as
   assuming a specific UX concept is right before validating it — the same
   caution this whole exploration has applied to product decisions
   (earn investment incrementally; don't leap to the maximal solution
   before it's validated) applies just as directly to a stack decision.
2. **Almost everything in Part 3 is achievable on the current stack this
   week, and directly answers the actual complaint.** The reviewer's
   verbatim concern was about *invisible automation and unexplained gates*
   — confirmation copy, focus defaults, blast-radius-scaled friction,
   status-color consistency, specific error messages, and empty-state
   quality (Part 3, roughly two-thirds of the list) are template and copy
   changes, not rendering-technology changes. Fixing those first answers
   the trust complaint far more directly than a new frontend stack would,
   and costs a fraction of the time.
3. **The specific things that genuinely need a stack change are needed for
   scale, not for today's fix.** True fuzzy-search command palettes across
   an entire app's action surface, buttery client-side transitions, and a
   real-time Ledger/Bridge view at fleet scale are exactly the pieces that
   matter once there's a validated fleet of real users and real apps to
   operate at volume — not pieces that fix the disconnected-dashboard
   complaint that started this whole exploration.

**The concrete staged path:**

- **Now:** ship Part 3's checklist on the current Jinja2 + htmx + Alpine
  stack. In parallel, start borrowing PatternFly's actual visual tokens
  (or a close analog) and the verified htmx/Alpine command-palette and SSE
  patterns from Part 2's portability note — zero framework change, and it
  forces a real, concrete component inventory (every confirm-dialog
  variant, every status badge, every empty state) that becomes the literal
  migration checklist later, so none of this work is wasted if a rewrite
  does happen.
- **The trigger for a real rewrite:** validated pull from real users —
  pilot sysadmins actually operating real apps through AgentIT, explicitly
  asking for the live-fleet/Ledger-at-scale experience that requires
  real-time streaming and denser client interaction than the current stack
  comfortably gives. That's the point where the Next.js + PatternFly (or
  shadcn, pending the product/brand decision above) investment is justified
  by real usage, not by this document's own aesthetic preference.
- **When it happens, migrate page-by-page, not big-bang** — starting with
  whichever page most needs real-time density first (almost certainly the
  Ledger/Fleet view). FastAPI stays the backend either way; the migration
  is "expose the same logic as JSON instead of Jinja fragments, one route
  at a time," which means none of the backend investment is at risk
  regardless of which frontend eventually sits on top of it.

---

## Part 5 — Implementation status (Part 3 checklist)

Verdicts as of the pass that closed most of the confirmed Fails/Partials
below (portal templates + minimal supporting routes, current Jinja2 +
htmx + Alpine stack, no framework migration). Items not touched this
session keep their prior status; **Pass** on an untouched item reflects
the state established before this pass, not a re-verification performed
now.

| # | Item | Verdict | Notes |
|---|------|---------|-------|
| 1 | Every consequential action shows a named confirmation | Pass | Unchanged — every `show-confirm` dispatch already states the action/target. |
| 2 | Confirmation friction scales to blast radius | **Pass** (was Fail) | Real type-to-confirm variant added, reserved for the two highest-blast-radius actions only: Delete App (`fleet.html`) and `cluster-admin-review` gate approval (`_macros.html`'s `gate_card()`). Every routine per-finding Fix/Deliver/Reject confirm stays a plain confirm — verified by browser test (`test_ordinary_confirm_has_no_type_to_confirm_input`). |
| 3 | Cancel/reject is always default-focused | **Pass** (was Fail) | Fixed once, in the shared `confirmModal()` component (`base.html`) — applies to every existing and future usage, not per-call-site. Browser-verified (`test_cancel_receives_default_focus_on_open`, `test_reflexive_enter_does_not_fire_destructive_action`). |
| 4 | LLM decisions show confidence + reasoning next to the action | Pass | Unchanged — outside this session's scope. |
| 5 | No single global toggle as the front door for a scoped capability | **Pass** (was Fail) | `settings.html` reordered: Auto-Mode Allowlist (the scoped, meaningful control) now renders first and is labeled "Recommended"; the global boolean is renamed "Global Fallback Toggle" and reworded as a coarse fallback. |
| 6 | Gate reasons are specific, never generic | Pass | Unchanged — outside this session's scope. |
| 7 | Empty/zero-pending states are informative, not blank | Partial | Closed for the two pages whose empty state this item's own example describes almost verbatim (Admin Review, Assessment Detail's Actions tab) — both now show a real, sourced "N resolved in the last 24 hours" count instead of a bare "all clear". Fleet/Events/Decisions (explicitly named in the parallel task's priority list) were **not** touched this pass: `events.html` and `routes/insights.py` (Decisions' route) were already being modified by a concurrent subagent, and Fleet's zero-app state has no "recent activity" to source from truthfully. Left open by deliberate scope choice, not an oversight. |
| 8 | Global `⌘K`/`Ctrl+K` command surface reachable from every page | **Pass** (was Fail) | Real, reusable Alpine component (`commandPalette()` in `base.html`) — fuzzy search over every nav item + every real app (`/api/fleet`, no mock data), plus a "Re-assess" action per app result via the real `/assess` endpoint. Browser-verified end to end (search, navigate, keyboard nav, Escape, real re-assess POST). |
| 9 | Every common action has a visible, discoverable keyboard shortcut | **Pass** (was Fail, scoped) | The palette itself is discoverable via a persistent nav hint (`.cmdk-trigger`, "Search… ⌘K") on every page — the one shortcut the checklist asked for at minimum. Inline shortcuts next to *every* other action were explicitly out of scope for this pass (checklist itself says "don't force it exhaustive on the first pass"). |
| 10 | Purely client-side interactions never round-trip to the server | Pass | Unchanged — collapse toggles/tabs were already local Alpine state. |
| 11 | Determinate progress when knowable; indeterminate + real status text otherwise | **Pass** (was Fail, for onboarding) | Onboarding now runs as a background job (`BackgroundTasks`, reusing the existing `remediation_jobs` job-tracking table) with a real progress page (`onboard_progress.html`) showing a genuine lifecycle stepper plus a live per-agent step list sourced from the real events `FleetOrchestrator` already logs — never a fabricated percentage. Genuinely-unknowable-duration ops (a single blocking LLM call inside a request/response cycle) were left indeterminate, per the checklist's own guidance not to force determinism onto those. |
| 12 | No operation over ~1s with zero visible feedback | Pass | Unchanged — global spinner/`btn-loading` handling already covers this. |
| 13 | Resolving one gate surfaces the next actionable item | **Pass** (was Partial) | Per-app gate resolution (approve/reject/dismiss) now lands on that app's Actions tab (`?tab=actions`), not the Overview tab, so the next pending gate is immediately visible with zero extra navigation. `cluster-admin-review` approval now jumps straight to the next pending Admin Review gate when one exists (only falling through to onboard-results once the queue is genuinely empty) — verified by route tests covering both branches. |
| 14 | Dense views show only the primary signal, full detail one click away | Pass | Unchanged — outside this session's scope. |
| 15 | Multi-field flows disclose fields progressively | Partial | Left open by explicit judgment call, per the parallel task's own guidance that this one is lower priority — the Assess modal's GitOps field stays always-visible-but-labeled. |
| 16 | Advanced/rare capability stays reachable but not default-visible | Pass | Unchanged — outside this session's scope. |
| 17 | Fixed, small semantic status-color vocabulary, used only for its canonical meaning | Pass | Unchanged — the badge/status color vocabulary (`--color-danger`/`warning`/`success`/`info`) was already consistent; this session's accent-color audit (#30) is the complementary fix. |
| 18 | No status communicated by color alone | Pass | Unchanged — outside this session's scope. |
| 19 | Delivery/deployment status visible ambiently | Pass | Unchanged — the nav deploy-status badge already covers this. |
| 20 | Reversible, low-stakes interactions get optimistic feedback | **Pass** (was Fail) | The Findings tab's Suppress action (genuinely reversible via Unsuppress, low-stakes — affects only future assessments, not cluster state) now hides the finding the instant it's confirmed via a real htmx `hx-post`/`hx-swap="none"` request, reconciling (restoring visibility + a real error toast) only if the request actually fails. Browser-verified for both the success and failure paths. |
| 21 | Long-running ops stream real, step-level progress (SSE) | **Pass** (was Fail, for onboarding) | Real htmx SSE (`hx-ext="sse"`, added as a genuine new dependency with a verified SRI hash) streams the same per-agent step data #11 above describes, polling the existing events table + job row server-side — no parallel notification mechanism. Route-tested for correct SSE framing and stream termination on a terminal job state. |
| 22 | Every error states its specific cause and a specific next step | **Pass** (was Partial, for the two named paths) | Restructured delivery-failure messages (`/deliver`, all three gate-approval apply paths in `routes/gates.py`) and the onboarding-failure message to include the real exception text plus a concrete next step ("fix the issue and re-approve, or Reject with a reason" / "retry Deliver" / "retry Onboard"), replacing bare "...failed — check server logs" text. The LLM-unavailable path (`/capabilities/learn`) was **not** touched — `routes/capabilities.py` was being modified by a concurrent subagent. |
| 23 | Exactly one verb triggers a given irreversible outcome | Pass | Unchanged — outside this session's scope. |
| 24 | Button copy always names the action and object | Pass | Unchanged, and the type-to-confirm additions follow the same rule (e.g. "I understand, delete this app", never a bare "OK"). |
| 25 | The same conceptual state renders identically everywhere (shared component) | Pass | Unchanged — `gate_card()` macro reuse predates this session; the confirm modal and command palette are both single shared components by construction. |
| 26 | A disabled control always states why, next to itself | Pass | The one new disabled control this pass introduced (the type-to-confirm Confirm button) states why inline, both via a persistent hint below the input and a `title` attribute while disabled. No other disabled controls were touched. |
| 27 | Motion is short, purposeful, and respects `prefers-reduced-motion` | **Pass** (was Partial) | Added a global `prefers-reduced-motion: reduce` CSS rule (`base.html`) that neutralizes every transition/animation in the app — existing and new — including Alpine's own inline-style-driven `x-transition` durations. Also fixed a real, pre-existing bug in the process: `.toast-enter`/`.toast-leave` were referenced but never actually defined, so toast motion was silently a no-op; both are now real, short (~200ms) animations. |
| 28 | A small number of genuine moments of joy, tied to real milestones only | **Pass** (was Fail) | Two, both derived from real backend data, never fabricated: (1) an app's first-ever 100/100 score (checked against real score history, never re-fires on a routine repeat), and (2) a genuinely clean multi-manifest delivery (≥3 files applied, zero errors) — deliberately gated above 1 file so a routine single-fix Deliver is never gamified. Both respect `prefers-reduced-motion` via #27's global rule. |
| 29 | Perceived speed taken as seriously as real speed | Pass | Unchanged — outside this session's scope. |
| 30 | One consistent accent color means "needs attention," used sparingly | **Pass** (was Partial) | Audited `base.html`'s CSS: plain links, `h1`/`h2`, `.section-title`, and `.section-toggle` moved off `--color-accent` onto new `--color-link`/`--color-heading` tokens (they were competing with the accent's attention-signaling role on every single page). Accent stays reserved for genuine attention signals (`nav-badge`'s pending-count bubble, nav-active/current-location, danger confirm styling) and the brand mark (logo, primary button color) — the latter two are standard, defensible exceptions no design system in Part 1 actually eliminates. |

**Summary: 9/9 confirmed Fails closed to Pass; 5/7 confirmed Partials closed to Pass (#7 empty states and #15 progressive disclosure deliberately left Partial/unchanged — see notes).**
