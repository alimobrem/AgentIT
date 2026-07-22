# Extension Model Unification — agents + skills + checks as one Markdown format

**Status: Phase 1 implemented (this doc; commits on
`feature/extension-model-unification-phase1`, 2026-07-18).** Supersedes
`docs/extension-model-unification-plan.md` and
`docs/extension-model-unification-plan-phase2.md` (both rescued from a
stranded scratch worktree the same day, both scoped to the narrower
checks-vs-analyzers problem the original spec explored — see "Reconciling
with the spec" below for why this doc's direction is broader). Written
fresh against the actual 2026-07-18 codebase rather than adapted from those
two stale plans, per explicit direction: a lot changed today (AutoMode
removal, Remediations removal, the Capabilities/Registry redesign with its
new Activate/Deprecate/Reactivate flow) and re-deriving a plan from scratch
against the real current code was judged faster and more trustworthy than
reconciling line-by-line against pre-redesign assumptions.

## The user's actual direction

> "so we have agent, skills, and checks, I still think we should unify it.
> This way they each are md files that define them, this gives us
> flexibility to update them easily and new ones, delete outdated ones,
> etc."

Concretely: agents, skills, and checks should all become **the same kind of
entity** — a Markdown file that defines what it does, when it triggers, and
what it does when triggered — so adding/updating/removing any of them is a
uniform, git-trackable file operation instead of three different code
patterns (Python classes for agents, YAML for checks, Markdown+frontmatter
for skills).

## Reconciling with the spec's "keep skills and checks separate" recommendation

`docs/extension-model-unification-spec.md` (committed `8cc5fcf`, read in
full for this doc) really does recommend, in its §2, "Option B — narrower
unification: fold what's actually declarative into `checks/*.yaml`, leave
skills and checks as two systems with two different jobs (detect vs.
remediate)" over "Option A — full unification: checks and skills become one
file format." A worker reporting "it explicitly recommends keeping skills
and checks separate" is accurate as far as it goes.

**But the spec's own scope, stated up front in its "Scope note," is
narrower than what the user is now asking for — it explicitly did not
re-examine the agents-vs-skills question at all**, deferring to a prior,
separate audit (`docs/agent-removal-readiness.md`) it treated as already
closed. The spec's Option-B reasoning (§2 point 1) is specifically about
*why merging checks' detection machinery into skills' remediation-matching
machinery* isn't worth it: `skill_engine.py`'s conflict-resolution,
platform-gating, and LLM-generation code is remediation-specific, and
forcing detection through it would mean either unused machinery or a
parallel code path. **That reasoning is still correct, and this Phase 1
respects it exactly** — the implementation below does *not* make
`SkillEngine.match()`/`generate()`/platform-gating apply to detection at
all; a `mode: detect` skill is explicitly walled off from every one of
those code paths (`Skill.matches()` returns `False` unconditionally for
`mode == "detect"`, `SkillEngine.generate()` returns `[]` immediately). What
*is* shared is only the parts that were never remediation-specific in the
first place: the file format (Markdown + YAML frontmatter), the lifecycle
states (`draft`/`active`/`deprecated`/`retired`), and the
Activate/Deprecate/Reactivate UI + git-PR persistence + `verify_skill()`
functional gate the Capabilities redesign built today for skills — none of
which the spec's Option-B argument was ever about.

So the honest framing is not "the user's direction contradicts the spec's
finding" — it's **the spec explored a narrower question (should checks'
*detection logic* merge into skills' *remediation matching logic*, verdict:
no) than the one the user is asking today (should checks and skills *share
one file format and one lifecycle*, independent of whether their matching
logic merges)**. This doc's Phase 1 is a real, working example of "yes to
the second question, no to the first" — a `mode: detect` skill is a full
peer of a `checks/*.yaml` file's declarative rule-matching (reusing
`check_engine`'s exact runners, per the spec's own Gap 1 idea, rediscovered
independently while designing this schema), expressed as one more file in
the same directory/format/lifecycle every other skill already uses, with
zero new remediation-side machinery. The agents question the spec deferred
is addressed by this doc directly (see "Agents: harder, and why" below) —
Phase 1 does not solve it (Python agents keep their real runtime logic,
correctly, for the same LLM/live-cluster/git-patch reasons the spec's own
"genuinely not declarative" analyzers (`security.py`, `infrastructure.py`)
had to stay Python), but does lay out the concrete, scoped path for
Phase 2+.

## What actually exists today (re-verified 2026-07-18, post-redesign)

| System | Format today | Loader | Lifecycle? | Count |
|---|---|---|---|---|
| Skills | Markdown + YAML frontmatter | `skill_engine.load_all_skills()` | Yes — `draft`/`active`/`deprecated`/`retired`, Activate/Deprecate/Reactivate UI (`portal/routes/capabilities.py`), git-PR-backed persistence, `verify_skill()` functional gate | 45 `.md` files, 12 domains, *before* this Phase 1's port below (46 after) |
| Checks | YAML, 5 rule types | `check_engine.load_checks()` | **None** — no status field, no activation flow, no verification gate; a check file is either present (live) or absent | 20 `.yaml` files, 7 dimensions, *before* this Phase 1's port below (19 after) |
| Python agents (one-shot) | Python classes | `agents/orchestrator.py::FleetOrchestrator`, registered in `agents/capabilities.py`'s hardcoded `AGENT_CLASSES` dict | None — code exists or doesn't; no draft/review step | 3: `cost`, `dependency`, `codechange` |
| Long-lived watchers | Python classes/loops | `watchers/*.py`, registered in `agents/capabilities.py`'s hardcoded `WATCHER_AGENTS` list | None (their own tick heartbeat is a liveness signal, not a lifecycle state) | 6: `vuln-watcher`, `slo-tracker`, `drift-detector`, `skill-learner`, `capability-scout`, `reassess-scheduler` |

**Correction to a claim that motivated earlier work on this problem:**
there are no Python agents left for `security`/`observability`/`cicd`/
`compliance`/`infrastructure`/`incident`/`release`/`retirement`/`chaos` —
all nine were removed once skills reached full template-fallback parity
(see `docs/agent-removal-readiness.md`, and `agents/capabilities.py`'s own
header comment, re-confirmed by reading the file directly today). Only 3
one-shot agents and 6 watchers remain, all correctly Python for reasons
detailed below, none of them a stale holdover nobody's looked at.

**The real gap the user is naming isn't "agents/skills/checks all do the
same thing" — it's "skills alone got a first-class Markdown format +
lifecycle + UI, and the other two didn't."** Checks are declarative and
`checks/*.yaml`'s 5 rule types are structurally almost identical to what a
skill's frontmatter could express — but a check has no draft/review step,
no deprecation, no Activate button; adding one requires knowing YAML lives
in `checks/`, editing Python nothing but still being a second convention to
learn. Agents are Python by necessity (LLM calls, live cluster access, git
patch application to AgentIT's own repo) but their *registration metadata*
— name, category, what they generate, which tier — is a hardcoded Python
dict (`AGENT_CLASSES`/`WATCHER_AGENTS` in `agents/capabilities.py`), not a
file a human could `git mv`/diff/PR to add or retire one.

## Phase 1 (implemented): checks become a peer of skills, sharing the exact same format and lifecycle

**Design decision: extend the skill Markdown format with a new `mode:
detect`, rather than inventing a parallel Markdown-checks format.** A
`checks`-shaped entity and a `skills`-shaped entity already share
everything structurally relevant (a name, a domain/dimension, a
description of what it does, a lifecycle) — the only real difference is
*what happens when it triggers*: a `template`/`llm`-mode skill's trigger is
"this keyword appears in a report's finding text" and its action is
"generate a K8s manifest"; a `detect`-mode skill's trigger is "run this
rule against the repo's file content" and its action is "produce a
Finding." Building a second Markdown schema for checks would have
recreated exactly the two-different-conventions problem this unification
exists to remove — one schema, one `mode` field deciding which of the two
jobs a given file does, is the unification.

### The new schema

```yaml
---
name: health-probes-check
domain: observability      # same meaning as a check's `dimension` today
version: 1
mode: detect                # new: template | llm | detect
triggers: []                 # always empty for detect (no remediation matching)
outputs: []                  # always empty for detect (no GeneratedFile)
severity: high                # new, detect-only: critical|high|medium|low|info
category: health               # new, detect-only: the Finding's category
description: No liveness/readiness probes detected in manifests   # new, detect-only
recommendation: Add livenessProbe and readinessProbe to all containers  # new, detect-only
rule:                            # new, detect-only: the declarative rule
  type: file_contains            # same 5 types check_engine.py already has
  pattern: livenessProbe         # str, or a list for OR semantics (see Gap 1 below)
  case_insensitive: false        # optional (see Gap 1 below)
status: active                  # same lifecycle every skill already has
---

# (Markdown body: human-readable docs, same convention as every other skill)
```

### What was built (real code, real tests, all passing)

1. **Gap 1 in `check_engine.py` — list-pattern OR matching + case-insensitive
   matching**, additive to the existing `CheckDefinition`/`_parse_check_file`/
   the 5 runner functions. `pattern` is now `str | list[str]` (a legacy
   scalar-pattern check file is unaffected — confirmed by the full
   pre-existing `test_check_engine.py` suite passing unchanged), and an
   optional `case_insensitive: bool` flag lowers both pattern and haystack
   before comparison in the two content-matching runners. This is the same
   Gap 1 the (now-superseded) rescued plans independently proposed for a
   different reason (porting `observability.py`'s analyzer) — rediscovered
   here because it's the natural primitive a `detect`-mode skill's `rule`
   needs too, and reusing `check_engine`'s own runners (rather than
   duplicating pattern-matching logic in `skill_engine.py`) means both
   formats get identical matching behavior for free.
2. **`Skill` dataclass gains `rule`/`severity`/`category`/`description`/
   `recommendation` fields** (all optional, default empty — zero effect on
   any of the 45 pre-existing template/llm-mode skill files), parsed by
   `load_skill()`.
3. **`Skill.matches()` and `SkillEngine.generate()` explicitly wall off
   `mode: detect`** — `matches()` returns `False` unconditionally (a detect
   skill's empty `triggers` would already make this true, but the explicit
   guard makes the exclusion a documented invariant, not an accident of
   empty-list semantics); `generate()` returns `[]` immediately. A
   detect-mode skill can never accidentally participate in remediation
   matching or generation, regardless of what a human puts in its
   frontmatter later.
4. **`skill_engine._skill_to_check_definition()` / `detect_check_definitions()`**
   — the bridge. Converts a `mode: detect` skill's `rule` into a real
   `check_engine.CheckDefinition` and runs it through `check_engine`'s
   existing runners (`_RUNNERS`, via `run_checks`/
   `run_checks_by_dimension_with_status`) — **no new rule-matching code was
   written**; a Markdown-defined rule and a legacy YAML check run through
   the literal same engine. `detect_check_definitions()` excludes `draft`
   (not yet reviewed) and `retired` (decommissioned) skills, mirroring
   `Skill.matches()`'s own draft/retired exclusion; a `deprecated` detect
   skill still runs (with a logged warning) — deprecating a detection rule
   is "flag this for review," not "stop enforcing it," a different
   lifecycle decision than deprecating a remediation skill nobody should
   generate from anymore.
5. **`verify_skill()` branches on `mode`** — `_verify_detect_skill()`
   checks the fields a detection rule actually needs (`rule`/`severity`/
   `description`/`recommendation`, plus that the rule actually compiles via
   `_skill_to_check_definition()`) instead of the remediation-shaped
   `triggers`/`outputs`/generate-against-a-fixture checks the original
   `verify_skill()` runs, which are meaningless for a skill that never
   produces a `GeneratedFile`. **This means the Capabilities page's
   existing Activate/Deprecate/Reactivate flow — built today, untouched by
   this change — already works correctly for `detect`-mode skills with zero
   changes to `portal/routes/capabilities.py`.** A human can draft a new
   detection rule as a `.md` file, and the exact same Activate button, the
   exact same git-PR-backed persistence, and the exact same functional
   verification gate that already exists for remediation skills applies to
   it, automatically.
6. **`runner.run_assessment()` merges detect-mode skill findings into the
   same pipeline as legacy checks** — a new optional `skills_dir` parameter
   (default: the real `skills/` directory, mirroring `checks_dir`'s
   existing default-resolution convention) loads skills, extracts
   `detect_check_definitions()`, and appends them to the same `check_defs`
   list legacy YAML checks already populate before `_merge_check_findings()`
   runs. A caller reading `check_results_out` (the portal's
   `AssessmentStore.save_check_results` path) cannot tell, and does not
   need to care, which format produced any given row — this is the "loader
   that can read the new unified format alongside the old one during a
   transition" the task asked for, and it is live in the real assessment
   path, not a separate parallel code path nobody calls.
7. **Fixed a latent bug in `runner._merge_check_findings()`**: a dimension
   whose *only* finding-producer is checks (true for any brand-new
   `detect`-mode skill in a domain with no analyzer at all — e.g.
   `chaos`/`cost`/`incident`/`dependency`/`release`/`retirement`, all
   skill-only domains today) would silently vanish from `report.scores`
   entirely (not a clean `100/100` row — *no* row) the moment every one of
   its checks passed, because the merge loop only ever populated
   `score_map` from `extra` (which only contains *failing*-check
   dimensions) and never accounted for a dimension with zero failures. Was
   invisible until now because every dimension with a check also has an
   analyzer that unconditionally returns a score. Fixed with an additive
   pass over every dimension `check_statuses` actually touched, giving it a
   clean `100/100` row if nothing else already did — covered by a
   dedicated test using a synthetic dimension name (so the test is valid
   regardless of which real dimension gains a detect-mode skill first).
8. **Ported the pilot entity**: `checks/observability/health-check.yaml`
   (chosen because it's the simplest, single-pattern legacy check with no
   analyzer duplicate to reason about) → `skills/observability/health-probes-check.md`,
   byte-for-byte the same rule, proven equivalent by a parity test
   (`TestDetectModeParity` in `tests/test_skill_engine.py`) before the YAML
   file was deleted in the same commit. This is the real, working
   demonstration of "delete an outdated entity, add its Markdown
   replacement" the user asked for — not a hypothetical.

### Test coverage added

- `tests/test_check_engine.py::TestListPatternMatching` — Gap 1's list
  patterns and case-insensitivity, scalar-pattern zero-behavior-change
  guard.
- `tests/test_skill_engine.py` — `TestDetectModeLoading`,
  `TestDetectModeNeverRemediates`, `TestSkillToCheckDefinition`,
  `TestDetectModeParity` (the pilot port's parity proof),
  `TestVerifyDetectSkill`, `TestRunAssessmentPicksUpDetectModeSkills`
  (including the vanishing-dimension regression test).
- `tests/test_all_skills.py::TestDetectModeSkills` — schema-level
  validation for every `mode: detect` skill on disk (mirrors
  `test_all_checks.py`'s role for legacy YAML checks): required fields,
  valid rule type/severity, empty triggers/outputs, and that the rule
  actually compiles via `load_skill()` + `_skill_to_check_definition()`.

Full relevant surface (`test_check_engine.py`, `test_skill_engine.py`,
`test_runner.py`, `test_all_checks.py`, `test_all_skills.py`) plus the full
repo suite (`pytest tests/ -q --ignore=test_real_repos.py
--ignore=test_browser.py --ignore=test_browser_critical.py
--ignore=test_live_cluster_e2e.py`, the exact CI invocation) were run
locally before every commit in this phase: **2775 passed, 293 skipped
(store/DB tests requiring a local Postgres this environment doesn't have),
0 failed.**

## Agents: harder, and why (not solved by Phase 1, scoped for Phase 2+)

Python agents (`cost`, `dependency`, `codechange`) and watchers
(`vuln-watcher`, `slo-tracker`, `drift-detector`, `skill-learner`,
`capability-scout`, `reassess-scheduler`) cannot become declarative
Markdown the way a check's pattern-matching rule can — this mirrors the
spec's own finding for `security.py`/`infrastructure.py` (LLM calls, live
cluster/Argo CD/GitHub API calls, date-math, git patch application to
AgentIT's own source tree — none of this is expressible as "does this
pattern appear"). That part of the gap is real and Phase 1 does not close
it, correctly, for the same reason the spec's own Option B leaves
`security.py`/`infrastructure.py` permanently Python.

**What *is* tractable, and scoped as Phase 2 below, is the metadata**:
`agents/capabilities.py`'s `AGENT_CLASSES`/`WATCHER_AGENTS`/
`AGENT_CAPABILITIES` are hardcoded Python dicts a human can only change by
editing Python and redeploying — there's no reason this registration
metadata (name, category, description, what it generates, which module/
class implements it, its polling interval) couldn't be a `mode: agent`
Markdown file's frontmatter, with a `code_ref` field pointing at the real
Python class (the same "declarative front, code reference for the
non-declarative part" pattern this codebase's own
`console-extensions.json`-style OpenShift plugin conventions already use
elsewhere) — while the actual `.run()`/watcher-loop logic stays exactly
where it is, in Python, untouched. This gets an agent the same lifecycle
(a human could draft a new agent registration, or retire one, as a file
diff) without pretending the agent's own logic is declarative.

## Backlog (not built in Phase 1 — sequenced, not attempted in one pass)

**Phase 2 — `mode: agent` registration metadata.** Add a `mode: agent`
Markdown schema (frontmatter: `name`, `category`, `code_ref` — a
`module:ClassName` string — `resource_tier`, a human-readable description)
for the 3 one-shot agents; a loader that replaces `AGENT_CLASSES`'s
hardcoded dict with one built from `agents/*.md` files (lazy-importing
`code_ref` exactly like `get_agent_class()` already does, just resolving
the module path from a file instead of a dict literal). Zero change to
`CostOptimizationAgent`/`DependencyAgent`/`CodeChangeAgent`'s own
`.run()` implementations. `agents/capabilities.py`'s `AGENT_CLASSES` becomes
a thin backward-compatibility shim (or is deleted outright once the
loader-based path is proven) rather than the source of truth.

**Phase 3 — `mode: watcher` for the 6 long-lived watchers.** Same idea as
Phase 2, applied to `WATCHER_AGENTS`: frontmatter carries `name`, `mode`
(loop kind), `interval`, description, `code_ref`; `watchers/__init__.py`'s
registration/heartbeat wiring is unchanged, only the *listing* of which
watchers exist moves from a Python list literal to a set of files. Lower
priority than Phase 2 (watchers change far less often than a one-shot
agent's registration might) — sequenced after, not before.

**Phase 4 — migrate the remaining ~19 legacy `checks/*.yaml` files to
`mode: detect` skills, dimension by dimension, each its own small,
parity-tested, revertible commit** — exactly the discipline
`docs/extension-model-unification-plan.md`/`-phase2.md` (rescued, now
historical) already modeled for the checks-vs-analyzers migration, applied
here to checks-vs-skills instead. Once every check is a `detect`-mode
skill, `check_engine.py`'s YAML-file-loading half (`load_checks`,
`_parse_check_file`) can be deleted — its rule-running half
(`_RUNNERS`/`run_checks*`) stays forever, since `detect_check_definitions()`
depends on it directly. **Do this dimension-by-dimension with a real parity
test per file, the same way this Phase 1 proved `health-check.yaml`'s
replacement before deleting it** — do not bulk-convert without individual
proof, especially for the mixed analyzers' checks (`cicd`/`compliance`/
`data_governance`/`ha_dr`), several of which the rescued
`-phase2.md` plan found are deliberately narrower or broader than their
analyzer counterpart for good, specific reasons that a mechanical bulk port
would silently lose.

**Phase 5 — Capabilities UI polish.** The Capabilities page already shows
every `detect`-mode skill (it renders `skills_by_domain`, sourced from the
same `load_all_skills()` call this phase's skills already go through) but
with no visual distinction from a remediation skill, and `checks_by_dimension`
still only reflects legacy YAML checks. A dedicated Phase 5 should: (a) add
a `mode` badge to the skill table so a human can tell detect from
template/llm at a glance, (b) fold `detect`-mode skills into the same
"Checks" count/section the page already shows for legacy YAML (or merge the
two into one "Detections" section entirely), (c) surface `rule`/pattern
content on the skill detail page the way a check file's YAML is currently
inspectable. Deliberately deferred out of Phase 1 because `capabilities.py`
was flagged as a very high concurrent-edit hotspot today (the
Activate/Deprecate/Reactivate flow itself just landed) — touching its
routes/templates today carries real merge-conflict risk for comparatively
low, UI-only value; the backend (Phases 1-4) is the part that actually
determines whether unification works.

**Phase 6 (optional, deferred) — revisit true single-format unification**
once Phases 2-4 land: at that point, `agents/`, `watchers/`, and `checks/`
would all be Markdown-with-a-`mode`-field, and the only Python-only
concepts left would be each mode's own execution logic (rule-runners for
`detect`, `.run()`/loop bodies for `agent`/`watcher`, template/LLM
generation for `template`/`llm`) — a natural point to ask whether even that
residual split is worth keeping separate loaders for, or whether one
`extension_loader.py` module should own every `mode`. Not scoped further
here; per the spec's own §2 point 4, this kind of consolidation is easier
to reason about *after* the individual pieces already work, not before.
