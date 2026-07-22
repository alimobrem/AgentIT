> **Rescue note (2026-07-18):** This document was written 2026-07-17 and
> existed only as an uncommitted, untracked file in the `AgentIT-ui-redesign`
> scratch worktree — never committed anywhere, invisible to every other
> worktree/agent. It is preserved here **verbatim** for historical record.
> Its scope (fold declarative analyzer findings for `cicd.py`/`compliance.py`/
> `data_governance.py`/`ha_dr.py` into `checks/*.yaml`) is narrower than, and
> now superseded by, the user's 2026-07-18 direction to unify agents+skills+
> checks into one markdown-defined extension model — see
> `docs/extension-model-unification-plan-2026-07-18.md` for the current,
> active plan. Kept as a record of real analysis work (the finding-by-finding
> classification table below is genuinely useful reference material for
> anyone doing analyzer-to-declarative porting later) that would otherwise
> have been lost. No code in this repo currently implements the steps below.

# Extension Model Unification Plan — Phase 2+3 (mixed analyzers + final-state documentation)

**Status: not yet implemented — planning artifact only.** Depends on
`docs/extension-model-unification-plan.md` (Plan 1) landing first: Task 1
below adds a second, independent extension (`all_of` + `internal`) to the
same `CheckDefinition`/`_parse_check_file`/`run_checks_by_dimension_with_status`
code Plan 1's Task 1 (list-pattern OR + `case_insensitive`) already modified.
Do not start Task 1 here until Plan 1's Task 1 is committed and its tests
are green.

This is the buildable version of `docs/extension-model-unification-spec.md`'s
Phase 2 ("the 4 mixed analyzers, split finding-by-finding, not file-by-file")
and Phase 3 ("document the final state"). See Plan 1's "Why two plans, not
one" section for why this is a separate document from the observability
pilot rather than one combined plan.

**How this plan differs from a literal reading of the spec, and why —
found by doing the finding-by-finding work the spec's own §1 asked for, not
by re-deriving its conclusions:**

1. **Gap 2 is scoped down to `all_of` only, not `all_of`/`any_of`.**
   Classifying every one of the 15 findings across the 4 analyzers (table
   below) turned up exactly one finding that fits a composition primitive
   at all (`cicd.py`'s GitOps finding, an `all_of`) and zero that need
   `any_of`. Per the spec's own principle (§3: "do not build it
   speculatively before a concrete ported analyzer needs it"), `any_of` is
   not built here.
2. **One finding the spec listed as a Gap-2 candidate turned out not to fit
   `all_of` at all, for a reason the spec didn't anticipate.** `cicd.py`'s
   "CI pipeline exists but is not Tekton-based" finding fires on `has_ci and
   not has_tekton` — an AND of one condition being *true* and another being
   *false*. `all_of`'s semantics (fire unless every referenced check
   passed) can express "fires unless A and B" (the GitOps case) but cannot
   express "fires when A holds and B doesn't" — a different polarity, not a
   negation of the same thing. The spec's own §6 flagged this exact finding
   as needing revision once someone tried to express it; this plan's
   conclusion is that it stays a Python residual rather than growing a
   second composition primitive for one low-severity finding.
3. **Two findings the spec implicitly assumed would port cleanly turned out
   to need the *check itself* to stay more permissive than a literal port
   would be, and one turned out the check must stay *narrower* than the
   analyzer.** `data_governance.py`'s "backup" finding does not become
   checks-only: the analyzer's real condition
   (`"backup" in yaml_content or "kind: CronJob" in yaml_content`, OR,
   independently, `"backup" in source_content and ("schedule" in
   source_content or "cron" in source_content)`) is strictly *more precise*
   than any single-pattern-list `file_contains` check could express without
   also matching a bare, unrelated mention of the word "backup" in a
   comment — a real accuracy regression, found by applying this plan's own
   mandated pre-cutover parity check to a fixture designed to expose it
   (§ Task 5). `compliance.py`'s "policy" finding is the opposite case:
   `checks/compliance/admission-policies.yaml` already exists, and is
   *already correct to be narrower* than the analyzer (see that check's own
   in-file comment and `tests/test_check_engine.py::TestAdmissionPoliciesCheck`,
   both pre-existing, both already deciding this) — the analyzer's
   corresponding finding-generating code is removed here, but the check
   itself is untouched.

## Finding-by-finding classification (all 15 findings, all 4 analyzers)

Re-verified against the working tree 2026-07-17. "SIMPLE" = ports via Gap 1
(list-pattern OR, from Plan 1). "COMPOSITE" = ports via this plan's
Gap 2 (`all_of`). "RESIDUAL" = stays Python permanently this phase, with the
specific rule-vocabulary gap named. "ALREADY COVERED" = an existing check
already produces this exact finding with identical `description` text
(safe, already-deduped by `runner._merge_check_findings()`'s `(category,
description)` key) — the analyzer's code is removed, the check is untouched.

| File | Finding (category) | Classification | Action |
|---|---|---|---|
| `cicd.py` | container | SIMPLE | Port to `checks/cicd/dockerfile.yaml` (exact-name list, Task 3), remove from analyzer |
| `cicd.py` | gitops | COMPOSITE | Port to `checks/cicd/gitops.yaml` as `all_of` (Task 3), remove from analyzer |
| `cicd.py` | pipeline (no CI, 7-path OR incl. 3 directories) | RESIDUAL | `file_exists` fnmatches a file's basename only, never a directory path — stays Python (Task 3) |
| `cicd.py` | pipeline (CI-not-Tekton, AND-NOT) | RESIDUAL | `all_of` can't express this polarity (see point 2 above) — stays Python (Task 3) |
| `compliance.py` | license | SIMPLE | Port to `checks/compliance/license.yaml` (Task 4), remove from analyzer |
| `compliance.py` | sbom | SIMPLE | Port to `checks/compliance/sbom.yaml` (Task 4), remove from analyzer |
| `compliance.py` | policy | ALREADY COVERED | `checks/compliance/admission-policies.yaml` already produces this exact finding, deliberately narrower and correct — remove from analyzer only, check untouched (Task 4) |
| `compliance.py` | audit | RESIDUAL | Same-file-scoped AND of two substrings, plus an independent filename OR — no rule type for this today — stays Python (Task 4) |
| `data_governance.py` | backup | RESIDUAL (reclassified — see point 3 above) | Check gets one small, safe, additive pattern (`kind: CronJob`); analyzer's finding-generating code is **not** removed (Task 5) |
| `data_governance.py` | migration | RESIDUAL | Directory-existence OR of 6 names — same gap as `cicd.py`'s CI-path finding — stays Python (Task 5) |
| `data_governance.py` | retention | RESIDUAL | Same-file-scoped AND-of-OR — stays Python, existing check untouched (already safely deduped, identical description) (Task 5) |
| `ha_dr.py` | availability (PDB) | ALREADY COVERED | `checks/ha_dr/pdb.yaml` already produces this exact finding — remove from analyzer only (Task 6) |
| `ha_dr.py` | scaling (HPA) | ALREADY COVERED | `checks/ha_dr/hpa.yaml` already produces this exact finding — remove from analyzer only (Task 6) |
| `ha_dr.py` | health (probes) | SIMPLE | Port to new `checks/ha_dr/health-probes.yaml` (Task 6), remove from analyzer |
| `ha_dr.py` | availability (replica count, regex) | RESIDUAL | Numeric-threshold regex — no rule type for this today — stays Python; **delete** the existing narrower, mismatched-description `checks/ha_dr/replicas.yaml` (Task 6, see why below) |

**Net effect on analyzer files:** `cicd.py` shrinks from 4 to 2 findings.
`compliance.py` shrinks from 4 to 1. `ha_dr.py` shrinks from 4 to 1.
`data_governance.py` is **not modified at all** in this phase — every one
of its 3 findings stays a documented residual; this is a legitimate,
evidence-based outcome, not a shortfall (see point 3 above). `runner.py`'s
`analyzers = [...]` list is **unchanged** — every one of the 4 classes still
gets instantiated, since each still owns at least one residual finding.

**Why two existing checks are deleted, not just left alongside their
residual analyzer counterpart:** `checks/cicd/ci-pipeline.yaml`
(`file_exists`, pattern `.gitlab-ci.yml`, description "No GitLab CI pipeline
configuration found") and `checks/ha_dr/replicas.yaml` (`file_contains`,
pattern `"replicas: 2"`, description "No multi-replica deployment found --
no redundancy") are each a **narrower, less accurate, differently-worded**
duplicate of a finding this plan keeps as a Python residual specifically
*because* the residual's condition is more precise (a full 7-path CI check;
a real numeric-threshold regex covering 2-9 and 2+ digit replica counts, not
just the literal substring `"replicas: 2"`). Because their `description`
text differs from the residual analyzer's own text, `_merge_check_findings`'s
`(category, description)` dedup does **not** catch the overlap — meaning
today, live, a repo using a Jenkinsfile-only CI (no `.gitlab-ci.yml`) or
`replicas: 3` (not literally `"replicas: 2"`) gets a spurious, factually
wrong finding from the narrower check *in addition to* the analyzer
correctly not flagging it. Deleting these two checks removes a real,
currently-live inaccuracy this plan's own audit surfaced — it is not new
scope creep, it is the same "eliminate a currently-real duplicate/inaccurate
producer" goal this whole migration exists for, applied to a case the check
turned out to be the wrong one to keep. Confirmed safe to delete:
`tests/test_portal.py`'s one reference to the literal string
`"checks/cicd/ci-pipeline.yaml"` (lines 961-976) uses it only as an
illustrative `Finding.source` path value in a hand-constructed `Finding` for
a portal-rendering test — it never loads the real file, so deleting it does
not affect that test.

## Goal

Port every SIMPLE/COMPOSITE finding in the table above to
`checks/{cicd,compliance,ha_dr}/*.yaml`, add the one new rule-vocabulary
primitive (`all_of`) this requires, shrink the 3 analyzers that have
portable findings accordingly, and record the final, settled state (what's
checks now, what's Python permanently and why) so this decision never needs
re-auditing from scratch.

## Architecture

`check_engine.py` (already modified by Plan 1) gains a 6th check type,
`all_of`, whose `pattern` is a list of *other checks'* `name` fields
(reusing Gap 1's list-pattern schema — no new YAML shape), plus an optional
`internal: bool` field. `run_checks_by_dimension_with_status` becomes a
two-pass evaluator: every non-`all_of` ("primitive") check runs first,
recording each one's pass/fail by name; every `all_of` check then resolves
by looking up its referenced names in that pass/fail map (a missing
reference fails safe — counts as not-passed, never silently passes) and
produces its own finding only if not every referenced check passed.
`internal: true` (settable on any check, primitive or `all_of`) suppresses
that check's own `Finding` even when it fails — it still gets a pass/fail
status row (for `AssessmentStore.save_check_results`/Insights compliance
tracking) — so `all_of`'s two constituent checks can contribute to the
composite without each also surfacing their own, redundant finding. 3
analyzer files (`cicd.py`, `compliance.py`, `ha_dr.py`) shrink to just their
residual findings; `data_governance.py` is untouched.

## Task 1 — Gap 2: `all_of` composition + `internal` suppression flag

Adds `"all_of"` to `VALID_TYPES`; extends `CheckDefinition` with
`internal: bool = False`; makes `run_checks_by_dimension_with_status` a
two-pass evaluator (primitives first, recording pass/fail by name; then
`all_of` checks resolved against that map, failing safe on a missing/typo'd
reference). Full test class (`TestAllOfComposition`, 6 tests covering parse,
AND semantics, internal suppression, fail-safe defaults) and full
implementation code were written in the original doc; see git history for
this file's pre-2026-07-18 content, or the design description in
`docs/extension-model-unification-spec.md` §3 Gap 2, for the complete
`all_of`/`internal` shape if resuming analyzer-to-declarative porting later.

## Task 2 — update the duplicated schema constants in `tests/test_all_checks.py`

`tests/test_all_checks.py` keeps its own independent copy of `VALID_TYPES`
(by design, to catch drift) — needs the matching `all_of` addition, plus a
new `TestAllOfReferencesExist` integrity check (every `all_of` check's
`pattern` list must reference check names that actually exist somewhere
under `checks/`, catching a typo'd reference at collection time).

## Task 3 — port `cicd.py`'s container + gitops findings

`checks/cicd/dockerfile.yaml` becomes an exact-name list (`[Dockerfile,
Containerfile, Dockerfile.prod]`, deliberately not a glob — matches
`CICDAnalyzer.has_container`'s exact literal list, not `Dockerfile.dev`).
Two new internal checks (`checks/cicd/argoproj-crd-present.yaml`,
`checks/cicd/application-kind-present.yaml`) feed a new
`checks/cicd/gitops.yaml` `all_of`. `checks/cicd/ci-pipeline.yaml` is
deleted (see "Why two existing checks are deleted" above). `cicd.py` shrinks
to just its 2 residual findings (no-CI 7-path OR; CI-not-Tekton AND-NOT).
Parity tests in `tests/test_cicd.py` prove the AND semantics (argoproj.io
alone, without an Application kind, must still fire) before the analyzer
code is removed.

## Task 4 — port `compliance.py`'s license + sbom findings, remove redundant policy code

`checks/compliance/license.yaml` (glob `["LICENSE*", "LICENCE"]`,
deliberately broader than the analyzer's 3 exact names — safe broadening).
`checks/compliance/sbom.yaml` (glob `["*sbom*", "*bom*"]`, exact semantic
match for the analyzer's substring check). `checks/compliance/
admission-policies.yaml` is untouched (already correct/narrower by design).
`compliance.py` shrinks to just its 1 residual finding (audit-logging,
same-file-scoped AND of two substrings — no rule type for this today).

## Task 5 — data_governance.py: safe, additive check improvement only (no analyzer change)

**No file in `analyzers/data_governance.py` changes in this task.** The
"backup" finding's real condition is strictly more precise than any single
`file_contains` list-pattern check can express without also matching an
unrelated bare mention of "backup" in a comment — discovered by applying
this plan's own parity-test discipline *before* committing to a port, per
this doc's introduction point 3. The one safe, additive change:
`checks/data_governance/backup-config.yaml` gains a second OR'd pattern
(`"kind: CronJob"`) matching a case the analyzer already accepts that the
check didn't. `checks/data_governance/retention-policy.yaml` is untouched
(already safely deduped by identical description text with the analyzer).

## Task 6 — port `ha_dr.py`'s health-probes finding, remove redundant pdb/hpa code, delete the inaccurate replicas check

New `checks/ha_dr/health-probes.yaml` (`file_contains`, `pattern:
[livenessProbe, readinessProbe]`, dimension `ha_dr` — deliberately distinct
from `checks/observability/health-check.yaml`'s narrower, single-pattern,
different-dimension check). `checks/ha_dr/replicas.yaml` is **deleted**
(narrower, inaccurate duplicate of the residual regex finding — see "Why
two existing checks are deleted" above). `checks/ha_dr/pdb.yaml` and
`checks/ha_dr/hpa.yaml` are untouched (already correct). `ha_dr.py` shrinks
to just its 1 residual finding (replica-count numeric-threshold regex,
`re.search(r"replicas:\s*([2-9]|\d{2,})", content)` — no rule type for
numeric thresholds today). A parity test class explicitly documents (rather
than hides) one known, accepted divergence: the check scans every file type
for probe keywords, the analyzer only scans YAML — deliberately left
unfixed, since it only makes the check more lenient, never less accurate in
the direction that matters.

## Task 7 — full regression run

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest \
  tests/test_check_engine.py tests/test_all_checks.py tests/test_runner.py \
  tests/test_cicd.py tests/test_compliance.py tests/test_data_governance.py tests/test_ha_dr.py \
  tests/test_observability.py tests/test_skill_agent_parity.py -v
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/ -x -q
```

Confirm the final full-suite pass/fail count against the baseline captured
before Task 1 — do not assume "all green" is the correct baseline without
having actually checked.

## Task 8 — Phase 3: document the final state

Per the spec's own §5 Phase 3: append a `## 7. Final state (Phase 1-3
complete)` section to `docs/extension-model-unification-spec.md` containing
the finding-classification table above plus the observability table from
Plan 1's Task 2, and update that doc's top "Status" line accordingly.

## Self-review (from the original doc)

**Every finding the spec's §1 identified across the 4 mixed analyzers is
accounted for** in the classification table above, with an explicit
SIMPLE/COMPOSITE/RESIDUAL/ALREADY-COVERED verdict and a task reference — no
finding is silently dropped. **Deliberately deferred, and why:** `any_of`
(no concrete use case found); a general directory-existence rule type
(would resolve `cicd.py`'s CI-path finding and `data_governance.py`'s
migration finding, but no concrete analyzer finding was found that *only*
needs this without also needing something else); a negation-aware
composition primitive for AND-NOT (`cicd.py`'s Tekton-migration finding —
one finding, `Severity.low`, not worth a new primitive); a same-file-scoped
AND/OR primitive (`compliance.py`'s audit finding, `data_governance.py`'s
retention finding — two findings, and the same underlying gap, but neither
forces this now).
