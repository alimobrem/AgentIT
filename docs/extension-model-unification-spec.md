# Unifying checks/analyzers/skills into one extension model — design spec

**Status: design spec, not yet implemented.** No code changes are made by
this doc. Written in the same style as `docs/ledger-design-spec.md`:
grounded in the *actual current* code (read directly, not designed against
an imagined system), with a phased, incremental rollout that never breaks
the working product mid-flight.

**Scope note — this doc is deliberately narrower than it might sound.**
The major-refactor item this responds to was "collapse skills/checks/
analyzers/remaining-Python-agents into one extension model." The
agents-vs-skills half of that question is **already answered** by
`docs/agent-removal-readiness.md` (audited 2026-07-12/13, days before this
doc): skills already cover every domain a Python agent used to own except
`codechange` (fundamentally not skill-shaped — see that doc's §3) and two
narrative-report gaps (`cost-report.md`, `dependency-report.md`) that are
explicitly accepted, documented feature gaps, not oversights. **This doc
does not re-litigate that.** It picks up the other, unaddressed half: do
**checks** (`check_engine.py`, `checks/*.yaml`) and **analyzers**
(`src/agentit/analyzers/*.py`) also belong in a unified model with skills,
and if so, how.

## 0. What actually exists today (verified by reading the code, not assumed)

Four systems produce output during the two real phases of this product's
lifecycle — **assessment** (detect problems, read-only) and **onboarding/
fix** (generate remediation, writes files):

| System | Phase | Format | Loader | Count |
|---|---|---|---|---|
| Analyzers | Assessment (detect) | Python classes, `analyze(repo_path) -> DimensionScore` | `runner.run_assessment()`'s hardcoded `analyzers = [...]` list | 7 classes (`security`, `observability`, `cicd`, `infrastructure`, `compliance`, `data_governance`, `ha_dr`) + `eol.py` (a helper `infrastructure.py` calls, not a standalone analyzer) |
| Checks | Assessment (detect) | Declarative YAML, 5 generic rule types | `check_engine.load_checks()` + `run_checks_by_dimension_with_status()`, merged into analyzer scores by `runner._merge_check_findings()` | 20 YAML files across the same 7 dimensions |
| Skills | Onboarding/fix (remediate) | Markdown + YAML frontmatter, keyword-`triggers` matched against a report's finding text | `skill_engine.SkillEngine` | 45 `.md` files across 14 domains |
| Python agents | Onboarding/fix (remediate) | Python classes, `.run() -> Result(files: list[GeneratedFile])` | `agents/orchestrator.py::FleetOrchestrator` (skill-covered domains skip the matching agent) | 3 surviving: `cost`, `dependency`, `codechange` |

**The real, verified redundancy is between checks and analyzers, not
between skills and anything.** Both score the *exact same* 7 dimensions
independently:

```
security        -> analyzers/security.py        AND checks/security/*.yaml (3 files)
observability   -> analyzers/observability.py    AND checks/observability/*.yaml (3 files)
cicd            -> analyzers/cicd.py             AND checks/cicd/*.yaml (3 files)
infrastructure  -> analyzers/infrastructure.py   AND checks/infrastructure/*.yaml (3 files)
compliance      -> analyzers/compliance.py       AND checks/compliance/*.yaml (3 files)
data_governance -> analyzers/data_governance.py  AND checks/data_governance/*.yaml (2 files)
ha_dr           -> analyzers/ha_dr.py            AND checks/ha_dr/*.yaml (3 files)
```

`runner.run_assessment()` (lines 74-92) runs every analyzer, *then* loads
and runs every check, then `_merge_check_findings()` deduplicates by
`(category, description)` so an overlapping analyzer+check pair doesn't
double-count — a merge step that exists *specifically because* these two
systems already cover the same ground independently.

## 1. Not every analyzer is the same shape — this is the crux of the design question

Reading all 7 analyzer classes plus `eol.py` in full (not just grepping
their `dimension = ` line) shows a real split — and a more fragmented one
than a first pass at `observability.py` alone suggested. **Correction
during this same audit pass:** an earlier draft of this doc claimed 5 of 7
analyzers were "trivially declarative," based on reading only
`observability.py` and assuming `cicd.py`/`compliance.py`/
`data_governance.py`/`ha_dr.py` shared its exact shape from their matching
`dimension = ` grep hit alone. Reading all four in full disproves that.
Only one analyzer is a clean, direct fit.

**Cleanly declarative (1 of 7): `observability.py`.** Its `CHECKS: dict[category,
(patterns: list[str], description, recommendation)]` shape is a pure "does
*any* of N keyword variants appear anywhere in the repo" per category (e.g.
`OTEL_PATTERNS = ["opentelemetry", "otel", "go.opentelemetry.io", ...]`,
5 variants for one "instrumentation" finding) — exactly `checks/*.yaml`'s
`file_contains` rule type, *except* `check_engine.py`'s `file_contains`
takes a single `pattern: str` (line 130-141), not a list. **This is a real,
concrete gap, not just data entry** — splitting one category into 5
separate `file_contains` checks (one per pattern) changes the semantics
from "fires if *none* of these appear" (OR, one finding) to "fires
independently per pattern" (up to 5 findings for what should be one).
Porting `observability.py` needs `file_contains` (and
`yaml_kind_exists`/`yaml_kind_missing`, same limitation) extended to accept
a list of patterns with OR semantics — but once that's done, this one
analyzer is a genuine 1:1 port.

**Not cleanly declarative, but for a different reason than
security/eol (3 of 7): `cicd.py`, `compliance.py`, `data_governance.py`,
`ha_dr.py`.** These don't call an LLM or do date math — but each has at
least one finding whose condition is a **boolean combination of two or
more independently-computed signals**, not "does pattern X appear":

- `cicd.py`: `has_gitops = has_argoproj and has_app_kind` (an AND of two
  separate YAML-content substring checks) feeds one finding; a second
  finding fires on `has_ci and not has_tekton` (AND-NOT of two other
  independently-computed flags, one of which is itself an OR over 7
  candidate CI-config paths).
- `compliance.py`: its audit-logging finding fires unless
  `"audit" in content.lower() and "log" in content.lower()` — a compound
  AND *within* the same file's content, not "does one pattern appear."
  (Its license/SBOM/policy findings *are* simple OR-of-patterns or
  OR-of-filenames, though — see below.)
- `data_governance.py`: its retention finding fires unless
  `"purge" in content_lower and ("retention" in content_lower or "days" in content_lower)`
  — an AND of one substring with an OR of two others, in the same content.
- `ha_dr.py`: its replica-count finding uses
  `re.search(r"replicas:\s*([2-9]|\d{2,})", content)` — a numeric-threshold
  regex, not a literal substring at all; its other three findings (PDB,
  HPA, health-probe presence) *are* simple single-pattern
  `file_contains`/`yaml_kind_exists` checks.

The honest shape here is **mixed within each file, not uniform across
it**: every one of these 4 analyzers has *some* findings that are already
simple enough for today's `check_engine.py` (once list-pattern OR support
lands) and *some* that need either (a) a genuinely new rule type (regex/
numeric-threshold matching for `ha_dr.py`'s replica count), or (b) a way to
express "this finding fires based on the AND/OR of two other conditions,"
which today's flat, single-condition-per-check model has no way to
represent at all — that's not a missing pattern-list, it's a missing
*composition* primitive.

**Genuinely not declarative for the LLM/computation reason (2 of 7, plus
the `eol.py` helper): `security.py`, `infrastructure.py`.**
- `security.py`: real regex-based secret scanning (`_get_match_line`,
  `_get_context_lines`, `_is_false_positive`) *plus* a live LLM call
  (`classify_secret`) per match to decide real-secret vs. false-positive,
  with the decision persisted as a durable, attributed record (see
  `llm_decisions.py`'s `secret-classify` decision type). No declarative
  YAML rule type can express "call an LLM and branch on its answer."
- `infrastructure.py` calls into `eol.py`'s `baseline_findings()`
  (Dockerfile/`.python-version`/`package.json` version parsing against a
  hardcoded EOL-date table, `_status_for()`'s date-math) and
  `llm_findings()` (LLM-driven EOL research). Same problem: date
  arithmetic and LLM calls aren't declarative-rule-shaped.

These two **must stay Python** regardless of what happens to the other
five — this mirrors `docs/agent-removal-readiness.md`'s own finding that
`codechange` must stay a standalone Python agent because the *capability*
itself (not the file format) doesn't fit the declarative model.

## 2. Two options, and which one to build

**Option A — full unification: checks and skills become one file format.**
Extend the skill Markdown+frontmatter format with a `mode: detect` (or
similar) that produces `Finding`s instead of `GeneratedFile`s, and port
`checks/*.yaml` (and whatever declarative analyzer findings §1 identifies)
into that format, retiring `check_engine.py` entirely in favor of
`skill_engine.py` handling both directions. One loader, one file format,
one place `skill_inventory.py` already snapshots for catalog-change
tracking.

**Option B — narrower unification: fold what's actually declarative into
`checks/*.yaml`, leave skills and checks as two systems with two different
jobs (detect vs. remediate).** Extend `check_engine.py`'s rule types (§3)
to support multi-pattern OR matching *and* a way to compose two or more
checks' pass/fail into one finding, port every finding that fits (all of
`observability.py`; the simple subset of `cicd.py`/`compliance.py`/
`data_governance.py`/`ha_dr.py`) into `checks/*.yaml`, and leave
`security.py`, `infrastructure.py` (LLM/date-math), plus whichever compound
findings turn out not to be worth a composition primitive, as permanent,
documented Python exceptions — the same shape `agent-removal-readiness.md`
already uses for `codechange`.

**Recommendation: Option B first.** Reasoning, grounded in what this audit
actually found:

1. **Skills and checks solve genuinely different problems and conflating
   them adds risk for no proven benefit.** Skills are matched by keyword
   `triggers` against a report's *finding text* and generate remediation;
   checks run structural rules against the *repo's file content* and
   produce findings. `skill_engine.py`'s matching, conflict-resolution
   (`_resolve_conflicts`), platform-gating (`has_api`), and LLM-generation
   machinery are all remediation-specific — none of it is needed for
   detection, and forcing detection through it would mean either leaving
   most of that machinery unused per detect-mode skill, or a second,
   parallel code path inside the same module. `check_engine.py` is 249
   lines and already does exactly what detection needs, cleanly.
2. **The actual, measured redundancy is checks vs. analyzers, not skills
   vs. anything.** §0's table shows checks and analyzers score the *same
   seven dimensions independently* — that's the concrete, verified
   duplication this refactor should remove. There's no analogous
   duplication between skills and checks/analyzers to justify merging
   those two.
3. **Lower blast radius, matching this project's own "ship incrementally"
   discipline** (`docs/ledger-design-spec.md` §5's phasing, this repo's
   real precedent). Option B touches `check_engine.py` (one new rule
   capability) and deletes 5 self-contained analyzer files one at a time,
   each independently testable and revertible. Option A means rewriting
   `skill_engine.py`'s loader/matching to handle two fundamentally
   different `mode`s, migrating 20 existing check files' semantics into a
   different frontmatter schema, and re-pointing `runner.py`'s call site —
   a much larger, harder-to-bisect change for a merge whose main
   justification (per point 1) isn't actually there.
4. **Option A is not foreclosed, just deferred.** If, after Option B ships
   and `checks/*.yaml` is the *only* declarative-detection format left (no
   more analyzer duplication), a future pass wants to go one step further
   and fold checks into the skill file format too (true single-format
   unification), that's a smaller, cleaner move at that point — starting
   from "one detection format + one remediation format" instead of
   "5 duplicated detection paths + 1 remediation format" as today.

## 3. The concrete rule-vocabulary gaps Option B must close first

Two independent, additive extensions to `check_engine.py` — neither
requires touching an existing check file's behavior.

**Gap 1: list-pattern OR matching.** `VALID_TYPES`'s five rule types
(`file_exists`, `file_contains`, `file_missing`, `yaml_kind_exists`,
`yaml_kind_missing`) all take a single `pattern: str`
(`CheckDefinition.pattern`, `_parse_check_file()` line 97). Every finding
identified in §1 as "cleanly declarative" needs "does *any* of N keyword
variants appear" (OR), not "does this one string appear."
- `CheckDefinition.pattern` accepts `str | list[str]` (YAML naturally
  supports this — a check file's `pattern:` becomes either a scalar or a
  block list, no new top-level key).
- Each runner (`_run_file_contains`, `_run_yaml_kind_exists`, etc.) treats
  a list as "matches if any element matches" — the same OR semantics
  `observability.py`'s `any(p in all_lower for p in patterns)` already has,
  relocated into the generic runner so every rule type gets it for free.
- New test class in `tests/test_check_engine.py` covering list-pattern OR
  semantics, landed before any check file depends on it.

**Gap 2: composing two or more checks into one finding.** §1 found real
findings (`cicd.py`'s GitOps/Tekton findings, `compliance.py`'s
audit-logging finding, `data_governance.py`'s retention finding) that need
an AND/OR of *other conditions*, not one pattern. Proposed 6th rule type,
`all_of`/`any_of`, whose `pattern` is a list of *other checks' `name:`
fields* (not a file pattern) — resolved after every primitive check in the
same run has already produced its pass/fail, combining their booleans:

```yaml
name: gitops-registered
dimension: cicd
severity: medium
category: gitops
type: all_of
pattern: [argoproj-crd-present, application-kind-present]
description: No GitOps configuration (Argo CD) detected
recommendation: Create Argo CD Application for GitOps delivery
```

This requires the two referenced checks (`argoproj-crd-present`,
`application-kind-present`) to exist as *intermediate* checks that
contribute to the `all_of`'s evaluation but shouldn't also each surface
their own user-facing finding (today, every check that fails produces a
Finding — there's no "internal signal, not a finding" concept). That's a
second, smaller schema addition (e.g. an optional `internal: true` flag) on
top of the `all_of` type itself.

**This composition primitive is real added complexity, not free** — it is
itself a small rule-expression system, and §5 treats it as optional,
attempted only after gap 1 ships and proves out, with an explicit
fallback ("leave the compound finding as a small, focused Python check"
run alongside the checks engine) if it doesn't pay for itself in practice.
Do not build it speculatively before a concrete ported analyzer needs it.

## 4. What happens to every existing consumer

| Consumer | Fate | Why |
|---|---|---|
| `runner.run_assessment()`'s `analyzers = [...]` list | Shrinks by one entry per ported analyzer, in the same commit that deletes that analyzer file. Never a batch removal. | Keeps each step bisectable; a regression in one ported dimension never blocks or gets confused with another. |
| `runner._merge_check_findings()`'s dedup-by-`(category, description)` | Unchanged, but exercised less over time as the analyzer side of each duplicate pair disappears — eventually a no-op for the 5 ported dimensions, since there's only one producer left. | It's dedup logic, not analyzer-specific; nothing about removing the *other* producer requires touching it. |
| `AssessmentStore.save_check_results` / `get_check_compliance()` (Insights page) | Unchanged. Ported analyzer-checks are just more rows in the same `check_results` table, keyed by `check_name` — the Insights page's fleet-wide pass-rate view gets *more* coverage, not a schema change. | `check_results` already models "one check, one pass/fail row," which is exactly what a ported analyzer check produces once it's a real check. |
| `Finding.source` field (`"analyzer:observability"` vs. `"check:checks/observability/....yaml"`) | Changes for ported dimensions (an analyzer-sourced finding becomes check-sourced) — anything keying UI/logic off the exact `source` string prefix must handle `"check:"` for domains that used to only ever say `"analyzer:"`. | Grep found `source` is read generically (`f.source` shown as a badge in `assessment_detail.html`'s finding list, `check_source`/suppression keyed off it in `/api/suppress`) — no code branches on "analyzer vs. check" specifically today, so this is a labeling change, not a logic change, but must be verified per dimension as it's ported, not assumed safe. |
| `agents/orchestrator.py`'s `PRIORITY_MATRIX`/`KNOWN_KIND_CONFLICTS` | Untouched. These resolve *skill vs. Python-agent* remediation conflicts (e.g. HPA vs. VPA) — orthogonal to the detection-side checks/analyzers question this doc addresses. | Confirmed by reading `orchestrator.py`'s own header comments (lines 52-90): this is exclusively about the 3 surviving Python agents vs. skills, already covered by `agent-removal-readiness.md`. |
| `skill_inventory.py`'s snapshot/diff (`skill-added`/`check-added`/`check-removed` events) | Already wired for exactly this — deleting an analyzer-equivalent check file (there are none yet; adding a *new* check file for a ported analyzer) is already tracked as a `check-added` event on the next hourly diff, visible on Events/Capabilities/Ledger without any change here. | This machinery was built generically ("catalog changes," not "skill changes specifically") — it already covers the checks side. |
| `tests/test_helm_templates.py`, chart, CI | Unaffected — this is an `src/agentit` + `checks/` + test change only, no chart/deployment surface. | Confirmed: none of `check_engine.py`, `analyzers/*.py`, or `checks/*.yaml` are referenced from `chart/` or `Containerfile`. |

## 5. Rollout sequencing

Mirrors `docs/ledger-design-spec.md` §5's own "each phase independently
shippable" discipline.

**Phase 0 — rule-vocabulary Gap 1 only (§3), no analyzer touched yet.**
Add list-pattern OR support to `check_engine.py`. Ship with its own test
coverage. Zero behavior change for any existing check file. Gap 2
(`all_of`/`any_of`) is deliberately *not* built in this phase — deferred to
Phase 2, and only if Phase 1 shows a real, recurring need for it.

**Phase 1 — port `observability.py` only, end to end, as the proof of
concept.** It's the one analyzer confirmed (by full reading, §1) to be a
clean 1:1 fit for Gap 1 alone — 6 categories, each a pure OR-of-patterns.
Write `checks/observability/*.yaml` for all 6, run both the analyzer and
the new checks side-by-side against a real repo fixture, assert identical
findings, *then* delete `analyzers/observability.py` and remove it from
`runner.run_assessment()`'s list in the same commit. This single port
validates Gap 1's implementation against a real, complete analyzer before
anything else depends on it.

**Phase 2 — the 4 mixed analyzers, split finding-by-finding, not
file-by-file.** For each of `cicd.py`, `compliance.py`,
`data_governance.py`, `ha_dr.py`: port every finding that's a simple
OR-of-patterns or OR-of-filenames (§1 identifies several per file — e.g.
`compliance.py`'s license/SBOM/policy findings) to `checks/*.yaml` using
Gap 1 alone. For the remaining compound/regex findings identified in §1
(`cicd.py`'s GitOps and Tekton-migration findings, `compliance.py`'s
audit-logging finding, `data_governance.py`'s retention finding,
`ha_dr.py`'s replica-count regex), evaluate Gap 2's `all_of`/`any_of`
primitive against the *specific* real case — if it cleanly expresses the
condition, build it and use it; if it would need a rule type more
elaborate than AND/OR of named checks (e.g. `ha_dr.py`'s numeric-threshold
regex needs a rule type of its own, not just composition), leave that one
finding in a small, focused, `dimension`-scoped Python module instead of
forcing it into YAML. Each analyzer file shrinks to only its
non-declarative findings, or is deleted outright if none remain — this is
4 independent, bisectable commits, not one.

**Phase 3 — document the final state.** Once Phase 2 settles (some
findings ported, some left as small Python residuals, some analyzer files
fully deleted), update this doc's status header and add a
`docs/agent-removal-readiness.md`-style table recording exactly which
findings are "checks now" vs. "Python permanently, and why" per dimension —
so this decision doesn't need re-auditing from scratch next time, the same
service `agent-removal-readiness.md` already provides for the agents side.

**Phase 4 — optional, deferred, not scoped by this doc.** Revisit Option A
(folding checks into the skill file format) only after Phases 1-3 land and
prove out, per §2 point 4.

## 6. Honest open questions this audit could not resolve

- All 7 analyzers were read in full during this pass (an earlier draft of
  this doc had only read `observability.py` and assumed the other 4 shared
  its shape — §1's correction note documents that mistake and the reread
  that fixed it). What remains unverified:
- Whether any of the 20 existing hand-written `checks/*.yaml` files already
  *duplicate* one of the analyzers' findings 1:1 (not just "same
  dimension," but "same specific finding") was not checked line-by-line —
  `runner._merge_check_findings()`'s dedup exists because *some* overlap is
  real, but the exact overlap map per finding was not built here. Phase 1
  and Phase 2's parity-testing step (run both, diff findings) will surface
  this concretely per finding rather than requiring it up front.
- `eol.py` findings' relationship to `infrastructure.py`'s *own* findings
  (are they merged, additive, ever conflicting?) was confirmed to exist via
  grep but not traced line-by-line in `infrastructure.py` itself — not
  needed for this doc's conclusions (both stay Python regardless), but
  worth knowing before anyone touches `infrastructure.py` for an unrelated
  reason.
- Gap 2's `all_of`/`any_of` design (§3) is a proposal sized against 4
  specific real findings, not implemented or test-driven here. Its exact
  shape (especially the `internal: true` intermediate-check flag) may need
  revision once Phase 2 tries to actually express `cicd.py`'s
  `has_ci and not has_tekton` finding, which is AND-*NOT*, not just AND —
  the proposal above doesn't yet show negation, and that gap should be
  resolved with a real check file in hand, not speculatively here.
