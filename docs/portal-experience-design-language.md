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
| **Fleet**, Admin Review, **Ledger**, Health, Insights | Primary nav (`#nav-primary`) | Ledger is a first-class primary destination |
| **Events** | Bell control → slide-over drawer | Full `/events` (+ DLQ) remains for filters/pagination |
| **Decisions**, Capabilities, Settings, Schedules | Account / main menu | Not primary-nav text links |

**MUST [check]** `base.html` keep Events as a bell/drawer (`events-bell`,
`#events-drawer`) rather than a primary-nav text link labeled only “Events”.

**MUST [check]** Decisions remain reachable from the account/main menu (not
reintroduced as a primary-nav peer of Fleet/Ledger).

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

**MUST [check]** never put **status / warning copy inside a `<button>`**
(including nested `.badge` chips such as “No dry run yet”). Put status in a
sibling chip, banner, or `role="status"` region.

**MUST [check]** interactive controls that look like buttons use `.btn` (or a
documented icon control: `.events-bell`, `.nav-toggle`, `.modal-close`,
`.cmdk-trigger`, `.collapse-toggle`, `.toast-close`). Do not invent ad-hoc
button skins with hard-coded hex colors.

**SHOULD** disabled-while-running: set `disabled` + `aria-busy="true"` (and/or
`.btn-loading`) on the control that started the async work.

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

## 6. Forms, empty states, badges

**Forms**

- **MUST** associate `<label for>` with control `id` (or wrap the control).
- **SHOULD** use `.form-narrow` / `.form-group` / `.form-label` for stacked forms.
- **MUST NOT** disable primary submit solely because of off-screen validation
  errors without an adjacent error message.

**Empty states**

- **MUST** use `.empty-state` with a reason + one next step (link or primary
  button). No guilt copy (“you haven’t…”).

**Badges**

- Severity: `.badge-critical` / `-high` / `-medium` / `-low` / `-info` / …
- Attention: `.badge-accent` only.
- **MUST [check]** `.badge` computed font-size ≥ 12px (use `var(--font-xs)` =
  `0.75rem`, never smaller literal sizes on `.badge`).

---

## 7. Onboarding / deliver step flow

Ordered primary path on `/assessments/{id}/onboard-results`:

1. **Dry Run** (secondary, `.btn-outline`) — preview only; nothing delivered.
2. **Apply** (primary, `.btn-green`) — the real delivery. Short labels preferred:
   **Apply** (direct) or **Open PR** (GitOps). Longer mechanism copy
   (“Apply to Cluster”, “Commit & Open PR”) **MAY** remain until a dedicated
   action-bar redesign lands, but both are the Apply step of this path — do
   not add a third competing primary.

**MUST [check]** Dry Run appear as its own control (not only inside Apply).

**MUST [check]** Apply label stay short; dry-run / warning status **MUST** live
*outside* the Apply button (chip or step-guide), never nested inside it.

**MUST** restate `delivery.confirmation_text()` in the confirm modal before
Apply fires.

**SHOULD** keep Per-Agent PRs / Download as secondary actions, visually quieter
than Dry Run → Apply.

> Concurrent redesigns of this action bar should keep Dry Run → Apply ordering
> and “no status inside buttons”; rebase onto this EDL rather than inventing a
> parallel primary.

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
| EDL-BTN-STATUS | MUST | No `.badge` / long warning text inside `<button>` |
| EDL-BTN-CLASS | SHOULD | Submit/action `<button>` uses `.btn` |
| EDL-CLICK-XDATA | MUST | `@click` has `[x-data]` ancestor in template |
| EDL-SAFE-URL | MUST | `href` with `pr_url` uses `\| safe_url` |
| EDL-NO-NATIVE-CONFIRM | MUST | No `window.confirm(` / `confirm('` / `confirm("` |
| EDL-MODAL-DIALOG | MUST | `.modal-overlay` → `role="dialog"` + `aria-modal` |
| EDL-MODAL-ESC | MUST | Dialog overlays bind Escape |
| EDL-BADGE-MIN | MUST | `.badge { font-size: … }` ≥ 12px / `var(--font-xs)` |
| EDL-TOKEN-HEX | SHOULD | No `style="…#hex…"` on `.btn` / links in templates |
| EDL-NAV-EVENTS | MUST | Events bell + drawer present in `base.html` |
| EDL-ONBOARD-ORDER | MUST | Onboard results: Dry Run control + Apply label; status outside Apply |
| EDL-DANGER-CLASS | MUST | `.btn-danger` defined when confirm modal uses it |
| EDL-TOASTS | MUST | `#toasts` present in `base.html` |

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
