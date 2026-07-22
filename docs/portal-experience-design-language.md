# AgentIT Portal — Experience Design Language (EDL)

**Status: normative for portal UI.** This is the machine-checked contract for
Jinja templates under `src/agentit/portal/templates/`. Broader UX research lives
in [`ux-design-requirements.md`](ux-design-requirements.md); IA history in
[`ui-redesign-proposal.md`](ui-redesign-proposal.md). When those conflict with
this EDL on shipped portal chrome, **this document wins**.

Conformance is enforced by `scripts/check_portal_edl.py` and
`tests/test_portal_edl.py` (see [Running checks](#running-checks)).

Rules use **MUST** / **SHOULD** / **MAY**. Rules tagged `[check]` are asserted
by the automated checker or the pytest suite.

---

## 1. Information architecture (masthead)

Shipped pattern (do not regress):

| Surface | Placement | Notes |
|---|---|---|
| **Ledger**, **Fleet**, Health, Insights | Primary nav (`#nav-primary`) | `/` redirects to Ledger (ops home) |
| **Cmd+K search** (`.cmdk-trigger`) | Right masthead cluster (`.nav-end`) with Events / Menu | MUST NOT be a center overlay or absolute-centered over primary nav |
| **Events** | Bell control → slide-over drawer | Full `/events` (+ DLQ) remains for filters/pagination — the system's real-time activity/audit-trail feed (every action the system takes, behind the scenes), not ops home |
| **Decisions**, Capabilities, Settings, Schedules | Account / main menu | Not primary-nav text links |

**Chrome:** primary `<nav>` and `.site-footer` are **fixed** (not sticky).
`base.css` sets `--nav-height` / `--footer-height` and pads `body` so
`#main-content` clears both. Events drawer / modals / toasts stay above
footer (`z-index`). Skip-to-content still targets `#main-content`.

Admin Review (a fifth primary-nav surface, an elevated RBAC queue for
`cluster-admin-review` gates) was retired 2026-07-18 along with that gate
type -- every gate type is per-app now, so there's no cross-app queue left
to give a dedicated surface to.

### Exclusive ownership (MUST NOT duplicate jobs)

Each primary surface owns exactly one job. Competing copy or badges are
regressions.

| Surface | Exclusive job | MUST NOT |
|---|---|---|
| **Ledger** (`/ledger`, home via `/`) | Morning inbox: Needs You, what happened, human gates needing action | Be demoted behind Fleet as the ops entry; hide the Needs You default |
| **Fleet** (`/fleet`) | Portfolio scoreboard: apps table, scores, Assess / Scan (Re-scan) / Delete | Own pending-ops inbox UI (no primary “N pending” badge/column competing with Ledger) |
| **Events** | System activity/audit-trail feed (every action the system takes, behind the scenes) + DLQ filters/pagination | Claim “ops home” or duplicate Ledger Needs You |
| **Decisions** | LLM decide-point audit log (menu) | Compete with Events for the chronological stream |
| **Health** | Live infrastructure telemetry | Become an activity/ops inbox |
| **Insights** | Fleet-wide aggregate analytics | Become an activity/ops inbox |

**MUST [check]** `base.html` keep Events as a bell/drawer (`events-bell`,
`#events-drawer`) rather than a primary-nav text link labeled only “Events”.

**MUST [check]** Decisions remain reachable from the account/main menu (not
reintroduced as a primary-nav peer of Fleet/Ledger).

**MUST** put the primary-nav gate badge (Needs You / pending app-owner gates)
on **Ledger**, not Fleet.

**SHOULD** unread/critical Events badge come from real `/api/events` data;
hide the badge when count is zero.

---

## 2. Button hierarchy

| Role | Classes | Use when |
|---|---|---|
| **Primary** | `.btn` (accent), or `.btn-green` / `.btn-action` for progressive “go” | One main next step on the view |
| **Secondary** | `.btn-outline` (+ optional `.btn-sm`) | Alternate / cancel / filter / download |
| **Danger** | `.btn-danger` (solid) or `.btn-danger-outline` | Destructive; prefer outline until confirmed |

**MUST** keep labels short: `{verb}` or `{verb} {noun}` (e.g. `Dry Run`,
`Fix`, `Apply`). Prefer ≤ 3 words.

**SHOULD [check]** visible button labels (`.btn-label` when present, else
button text) stay ≤ 3 words and ≤ 48 characters. Long mechanism copy belongs
in a toast, confirm modal, or sibling status region — not the CTA.

**MUST [check]** never put **status / warning copy inside a `<button>`**
(including nested `.badge` chips such as “No dry run yet”). Put status in a
sibling chip, banner, or `role="status"` region.

**SHOULD [check]** interactive controls that look like buttons use `.btn` (or a
documented icon control: `.events-bell`, `.nav-toggle`, `.modal-close`,
`.cmdk-trigger`, `.collapse-toggle`, `.toast-close`, `.user-menu-trigger`,
`.events-drawer-close`). Do not invent ad-hoc button skins with hard-coded hex
colors.

**SHOULD** disabled-while-running: set `disabled` + `aria-busy="true"` (and/or
`.btn-loading`) on the control that started the async work. `base.html` applies
this globally on form `submit` / htmx requests; long-running CTAs SHOULD also
include a `.htmx-indicator` spinner so `.btn-loading` has a named verb to show.

---

## 3. Shared feedback (loading / success / error)

| Channel | When | Pattern |
|---|---|---|
| **Toast** | Short, non-blocking outcome | `#toasts` / `showToast(...)` |
| **Banner (`.alert-*`)** | Page-level durable message | `.alert-success` / `.alert-warn` / `.alert-error` / `.alert-info` |
| **Inline status** | Bound to a control or section | `role="status"` + `aria-live="polite"` |
| **Button loading** | Form / htmx in flight | `.btn-loading`, `aria-busy="true"` |

**MUST [check]** `base.html` define toast UI (`#toasts`) and confirm modal
feedback path.

**MUST** prefer determinate or step-named progress when duration is known
(onboarding SSE); otherwise pair spinners with a verb (“Running…”,
“Assessing…”).

**SHOULD [check]** pages with primary async POST actions keep a live region
(`role="status"` or `#toasts`) available from `base.html` (inherited).

---

## 4. Banners vs chips vs buttons

| Component | Purpose |
|---|---|
| **Banner (`.alert`)** | Page- or section-scoped message that should be read before acting |
| **Chip / badge (`.badge*`)** | Compact status or count; not clickable by itself |
| **Button (`.btn*`)** | Triggers an action |

**MUST** not use a button as a status display, and not use a badge as the only
hit target for a destructive action (pair badge + labeled control).

**MUST [check]** attention (“needs you”, pending gates) use `.badge-accent`
(`--color-accent`), not severity badges (`.badge-medium` / `.badge-warning`).

---

## 5. Modal patterns

Shared confirm: `#confirm-modal` in `base.html`. Assess / other overlays use
`.modal-overlay`.

**MUST [check]** every `.modal-overlay` (and the Events drawer panel) expose
`role="dialog"` and `aria-modal="true"`.

**MUST [check]** dialogs support Escape to dismiss
(`@keydown.escape` / `@keydown.escape.window`).

**MUST** set `aria-labelledby` (preferred) or `aria-label` on the dialog.

**MUST** focus Cancel (or the least destructive control) when a confirm opens;
Confirm copy names the action (`Deliver Now`, `Delete …`), never bare `Yes`.

**MUST [check]** no native `window.confirm(` / `confirm('…')` for portal UX —
use `$dispatch('show-confirm', …)`.

**SHOULD** trap focus while open (Events drawer already does; confirm/command
palette follow the same expectation).

---

## 6. Forms, filters, empty states, badges

**Forms**

- **MUST** associate `<label for>` with control `id` (or wrap the control).
- **SHOULD** use `.form-narrow` / `.form-group` / `.form-label` for stacked forms.
- **MUST NOT** disable primary submit solely because of off-screen validation
  errors without an adjacent error message.

**Filters (list / log pages)**

List and log pages that expose query filters (Decisions, Events, Ledger, and
any future peer) use a **compact filter toolbar**, not the primary `.action-bar`
(which is for page actions) and not stacked full-width form cards.

| Role | Classes | Notes |
|---|---|---|
| **Filter bar** | `.filter-bar` on the GET `<form>` | One horizontal wrap row; compact control height |
| **Field** | `.filter-field` wrapping label + control | Small label above (or `aria-label` on the control) |
| **Wide text** | `.filter-field-wide` on the field or input | Search `q` only — still capped, never full viewport |
| **Actions** | `.filter-actions` | Filter submit + Clear/reset as `.btn btn-sm btn-outline` |
| **Mobile shell** | `.filter-panel` + `<summary class="filter-panel-summary">` | Optional `<details open>`; summary hidden on desktop |

**MUST [check]** GET filter forms on Decisions / Events / Ledger use
`class="filter-bar"` (not `class="action-bar"`).

**MUST [check]** `base.html` define `.filter-bar` styles that override the
global `input, select { width: 100% }` rule so filter controls stay compact
(`width: auto`, bounded `max-width`, shared control height / `font-size`).

**MUST** keep control heights consistent inside `.filter-bar` (shared padding /
line-height); do not mix oversized selects with tiny buttons.

**MUST** provide a Clear / reset affordance when any filter query param is
active (link back to the bare list path, same secondary button classes as
Filter).

**SHOULD** collapse the bar behind a “Filters” disclosure on narrow viewports
(`.filter-panel` / `.filter-panel-summary`) when there are more than two
controls; desktop always shows the horizontal wrap row.

**Do**

```html
<details class="filter-panel" open>
  <summary class="filter-panel-summary">Filters</summary>
  <form method="get" action="/decisions" class="filter-bar" role="search"
        aria-label="Filter decisions">
    <div class="filter-field">
      <label for="filter-decision-type">Decision type</label>
      <select id="filter-decision-type" name="decision_type">…</select>
    </div>
    <div class="filter-actions">
      <button type="submit" class="btn btn-sm btn-outline">Filter</button>
      <a href="/decisions" class="btn btn-sm btn-outline">Clear</a>
    </div>
  </form>
</details>
```

**Don't**

- Put filter `<select>` / `<input>` in `.action-bar` or `.stat-card` stacks
  (inherits full-width form chrome → oversized controls).
- Use default stacked `.form-group` widths for list filters.
- Omit Clear when filters are applied, or invent a third button skin for reset.

**Empty states**

- **MUST** use `.empty-state` with a reason + one next step (link or primary
  button). No guilt copy (“you haven’t…”).

**Badges**

- Severity: `.badge-critical` / `-high` / `-medium` / `-low` / `-info` / …
- Attention: `.badge-accent` only.
- **MUST [check]** `.badge` computed font-size ≥ 12px (use `var(--font-xs)` =
  `0.75rem`, never smaller literal sizes on `.badge`).

---

## 7. Onboarding / Scan delivery results

`/assessments/{id}/onboard-results` is a **results** page, not a second
deliver product. **Scan** (assess → generate → `auto_delivery`) is the only
UI path that opens pull requests.

Primary framing:

1. **Pull Requests** — cards/links for every PR Scan opened (or will open).
   Copy steers the human to **review and merge on GitHub**.
2. **Opened by Scan** status when `pr_opened_count > 0` — no competing
   Commit / Per-Agent CTAs.
3. **Retry Scan delivery** (`.btn-outline`, `data-action="retry-scan-delivery"`)
   — only when the latest onboard job is `needs_attention` **and** no PR is
   open yet. Posts to the same `.../onboard-results/run-validation` pipeline
   Scan already uses; must not read as a separate "Commit" product.
4. **Download** — secondary only.

**MUST [check]** Onboard Results **MUST NOT** render **Commit & Open PR**,
**Per-Agent PRs**, or a deliver-choice step (“One PR for everything…”).

**MUST [check]** When PRs are open, status copy mentions merge on GitHub
(not “choose a deliver option”).

**SHOULD** keep **Download** as the only always-present secondary action.

> Concurrent redesigns must not reintroduce a manual Commit/Per-Agent stack
> beside Scan auto-delivery.

---

## 8. Typography, spacing, color tokens

**MUST [check]** new colors/spacing/type in templates use existing CSS
variables from `base.html` `:root` (`--color-*`, `--space-*`, `--font-*`,
`--radius-*`). No new hard-coded hex on interactive chrome in templates
(inline `style="color:#…"` on buttons/links).

**MUST** keep `--color-accent` reserved for attention / nav-active signals
(see `:root` comment in `base.html`).

**MUST [check]** body/UI text not declare `font-size` below 12px (0.75rem);
`--font-xs` is the floor for badges and meta.

---

## 9. Accessibility baselines

**MUST** visible focus styles remain for keyboard users (do not `outline: none`
without a replacement).

**MUST** icon-only controls have `aria-label`.

**MUST [check]** Alpine `@click` / `x-on:click` handlers sit inside an `[x-data]`
ancestor (same template scope). Dead handlers are bugs.

**MUST [check]** external/`pr_url` (and other user-influenced) `href`s use the
`safe_url` Jinja filter.

**MUST** dialogs and drawers expose dialog semantics (see §5).

**MUST** minimum readable text size 12px (see §6 / §8).

---

## 10. Machine-checkable rule index

| ID | Severity | Checker / test |
|---|---|---|
| EDL-BTN-STATUS | MUST | No `.badge` / status-warning text inside `<button>` |
| EDL-BTN-STATUS | SHOULD | Visible label ≤ 3 words / ≤ 48 chars |
| EDL-BTN-CLASS | SHOULD | Interactive `<button>` uses `.btn` (or documented icon control) |
| EDL-CLICK-XDATA | MUST | `@click` has `[x-data]` ancestor in template |
| EDL-SAFE-URL | MUST | `href` with `pr_url` uses `\| safe_url` |
| EDL-NO-NATIVE-CONFIRM | MUST | No `window.confirm(` / `confirm('` / `confirm("` |
| EDL-MODAL-DIALOG | MUST | `.modal-overlay` → `role="dialog"` + `aria-modal` |
| EDL-MODAL-ESC | MUST | Dialog overlays bind Escape |
| EDL-BADGE-MIN | MUST | `.badge { font-size: … }` ≥ 12px / `var(--font-xs)` |
| EDL-TOKEN-HEX | SHOULD | No `style="…#hex…"` on `.btn` / links in templates |
| EDL-NAV-EVENTS | MUST | Events bell + drawer present in `base.html` |
| EDL-ONBOARD-ORDER | MUST | Onboard results: no Commit/Per-Agent CTAs; Retry Scan delivery only for needs_attention; Download secondary |
| EDL-DANGER-CLASS | MUST | `.btn-danger` defined when confirm modal uses it |
| EDL-TOASTS | MUST | `#toasts` present in `base.html` |
| EDL-FILTER-BAR | MUST | Decisions / Events / Ledger GET filters use `.filter-bar` (not `.action-bar`) |
| EDL-FILTER-CSS | MUST | `base.html` defines compact `.filter-bar` control sizing |

---

## Running checks

```bash
# Full EDL suite (CI-friendly; no Playwright)
uv run pytest tests/test_portal_edl.py -q

# Checker only (same rules as the static half of the suite)
uv run python scripts/check_portal_edl.py

# Optional: browser-level modal/button checks (local; CI ignores test_browser.py)
uv run pytest tests/test_browser.py -k "edl or modal or escape or onboard" -q
```

Agents and humans changing portal templates: read this file first
(`.cursor/rules/portal-edl.mdc` points here).
