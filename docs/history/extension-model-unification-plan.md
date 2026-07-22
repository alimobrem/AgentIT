> **Rescue note (2026-07-18):** This document was written 2026-07-17 and
> existed only as an uncommitted, untracked file in the `AgentIT-ui-redesign`
> scratch worktree — never committed anywhere, invisible to every other
> worktree/agent. It is preserved here **verbatim** for historical record.
> Its scope (fold declarative analyzer findings into `checks/*.yaml`,
> Option B from the spec below) is **narrower than, and now superseded by**,
> the user's 2026-07-18 direction to unify agents+skills+checks into one
> markdown-defined extension model — see
> `docs/extension-model-unification-plan-2026-07-18.md` for the current,
> active plan. This file is kept for its still-potentially-useful ideas
> (e.g. the `CheckDefinition` list-pattern/case-insensitive schema) and as a
> record of work that would otherwise have been lost. No code in this repo
> currently implements the steps below as written; do not treat the "commit"
> shell commands in this doc as already having happened.

# Extension Model Unification Plan — Phase 0+1 (Gap 1 + observability pilot cutover)

**Status: not yet implemented — planning artifact only, per the request that
produced this doc.** Picks up `docs/extension-model-unification-spec.md`
("Status: design spec, not yet implemented") at exactly the point it stops:
that doc analyzes the problem and recommends **Option B** (fold what's
declarative into `checks/*.yaml`, leave `security.py`/`infrastructure.py`
permanently Python) but stops before writing a single line of code. This doc
is the buildable version of that spec's own **Phase 0** (§5, "rule-vocabulary
Gap 1 only") and **Phase 1** ("port `observability.py` only, end to end, as
the proof of concept") — nothing more. The 4 mixed analyzers
(`cicd.py`/`compliance.py`/`data_governance.py`/`ha_dr.py`, the spec's
Phase 2) are a separate, larger, higher-uncertainty body of work with its own
document: `docs/extension-model-unification-plan-phase2.md`. See "Why two
plans, not one" below for the reasoning.

Every fact below was re-verified directly against the working tree during
this planning pass (2026-07-17), not copied from the spec — where the spec's
own description turned out stale or incomplete, that's called out explicitly.

## Why two plans, not one

The spec's own §1 already found that `observability.py` is qualitatively
different from the other 4 "mixed" analyzers: it is the **only one of the 7**
that is a clean, uniform, 1:1 fit for list-pattern OR matching alone — no
analyzer in this repo needs a composition primitive to be fully ported. The
other 4 analyzers each mix simple OR-of-patterns findings (portable the same
way) with genuinely compound findings (AND, AND-NOT, same-file-scoped
AND-of-OR, and one numeric-threshold regex) that need either a new
composition rule type this repo has never built before, or a permanent
Python residual, decided finding-by-finding. That is a fundamentally
different, larger, and more uncertain scope of work than "port one uniform
analyzer end-to-end," with a different (larger) rule-vocabulary addition
(Gap 2's `all_of`, scoped down from the spec's own `all_of`/`any_of`
proposal — see Plan 2 for why). Forcing both into one plan would mean Plan
2's open questions (does the AND-NOT case even get a composition primitive,
or stay Python forever?) block Plan 1's proof-of-concept from shipping and
being verified in production first, which is exactly the incremental,
bisectable discipline both this spec and `docs/ledger-design-spec.md` commit
to. Each plan below produces real, working, tested software on its own and
neither blocks on the other except in the direction Plan 2 → depends on →
Plan 1 (Gap 2 is additive on top of Gap 1's schema change; Plan 2 does not
start until Plan 1's Task 2 has landed and its own tests are green).

The Python-agents-vs-skills question the original prompt for this work also
raised is **not** a third plan — see "Explicitly out of scope" below.

## Goal

Give `check_engine.py` list-pattern OR matching and case-insensitive content
matching, then use those two primitives to port every finding in
`analyzers/observability.py` to `checks/observability/*.yaml` with proven,
tested parity, and delete that analyzer — the first complete removal of one
of the two systems the spec's §0 confirms redundantly score the same 7
dimensions today.

## Architecture

`check_engine.py`'s `CheckDefinition.pattern` becomes `str | list[str]`
(scalar YAML stays scalar; a check that needs OR semantics writes a YAML
block list) and gains an optional `case_insensitive: bool` field; every rule
runner (`_run_file_exists`, `_run_file_contains`, `_run_file_missing`,
`_run_yaml_kind_exists`, `_run_yaml_kind_missing`) is extended to treat a
list pattern as "matches if any element matches," with `case_insensitive`
lowering both content and patterns before comparison in the two
content-matching runners. `runner.py`'s `_merge_check_findings` gains a
second pass that guarantees every dimension covered by *any* check gets a
`DimensionScore` row even when every one of its checks passes (today, a
dimension only appears via checks if at least one check fails — fine while
every dimension also has an analyzer that unconditionally returns a score,
but silently broken the moment a dimension's *only* producer is checks, which
observability becomes at the end of this plan). Once both land,
`analyzers/observability.py`'s 6 categories are ported 1:1 to 6
`checks/observability/*.yaml` files (3 upgraded/new to use the list pattern,
3 already correct), a parity test proves identical findings against the
analyzer across 4 fixture shapes, and the analyzer is deleted in the same
commit its removal from `runner.run_assessment()`'s `analyzers = [...]` list
lands.

## Tech Stack

Python 3.12+, pydantic 2.7+ (`agentit.models`), PyYAML (`yaml.safe_load`),
pytest 8.0+ with `pytest-asyncio` (none of this plan's tests are `async` —
none of the touched modules do store I/O), no live cluster or Postgres
required for any test in this plan (verify with
`KUBECONFIG=/tmp/nonexistent-path pytest tests/test_check_engine.py
tests/test_observability.py tests/test_runner.py tests/test_all_checks.py`).

## Global Constraints (copied verbatim from what this pass read)

**`CheckDefinition` schema today** (`src/agentit/check_engine.py:34-101`),
before this plan's changes:

```python
class CheckDefinition:
    __slots__ = ("name", "dimension", "severity", "category", "check_type", "pattern",
                 "description", "recommendation", "source_path")

    def __init__(self, name, dimension, severity, category, check_type, pattern,
                 description, recommendation, source_path) -> None:
        ...
```

`_parse_check_file()` requires exactly these keys in every YAML check file:
`name`, `dimension`, `severity`, `category`, `type`, `pattern`,
`description`, `recommendation` — `type` must be one of `VALID_TYPES =
{"file_exists", "file_contains", "file_missing", "yaml_kind_exists",
"yaml_kind_missing"}`; `severity` (case-insensitive) must be one of
`critical`/`high`/`medium`/`low`/`info`. Today `pattern` is always coerced to
`str(data["pattern"])` (line 97) — this is exactly Gap 1's target.

**Skill Markdown frontmatter schema** (`src/agentit/skill_engine.py:169-223`,
`load_skill()`) — not touched by this plan, cited here only because
`docs/extension-model-unification-spec.md` §2 point 1 uses it as the reason
*not* to merge checks into this format: required keys `name`, `domain`,
`version`, `triggers` (list), `outputs` (list); optional `property`, `mode`
(default `"template"`), `status` (default `"active"`, one of
`active`/`deprecated`/`retired`/`draft`), `superseded_by`,
`deprecated_reason`, `conflicts_with`, `requires_crd`, `source`,
`created_at`. Body must contain a fenced ` ```yaml ` block for
`_extract_template()` to find in template mode.

**Repo doc convention** (this doc follows it): flat files directly under
`docs/`, kebab-case, suffixed by kind — `-spec.md` (design, not yet built),
`-plan.md` (buildable), `-readiness.md` (audit). Status lives in a bolded
paragraph at the very top and is *updated in place* as phases land (a
`## Progress update: ...` section is appended, the original body is never
rewritten to pretend the old state didn't happen) — see
`docs/kafka-hardening-plan.md`, `docs/postgres-migration-plan.md`,
`docs/ledger-design-spec.md` for the exact pattern this doc will follow once
work starts. **Do not** use `docs/superpowers/plans/YYYY-MM-DD-*.md` — that
path exists in this repo (`docs/superpowers/plans/2026-07-15-autonomous-self-improve-dogfood.md`)
but is a different, generic workflow's convention, not this program's.

**CLAUDE.md rules that bind every task below:** never `except Exception:
pass` (always `logger.warning`/`logger.exception`); never `# type: ignore`;
shared analyzer utilities (`IGNORED_DIRS`, `iter_text_files`, `iter_yaml_files`,
`calculate_score`, `is_ignored`) live in `analyzers/base.py` — import, never
duplicate. Tests: `KUBECONFIG=/tmp/nonexistent-path` avoids multi-minute
hangs from unconditional `kube.list_custom_resources` calls elsewhere in the
suite (irrelevant to this plan's own files, but harmless/recommended for any
full-suite run).

## Explicitly out of scope (and why)

- **The 4 mixed analyzers** (`cicd.py`, `compliance.py`, `data_governance.py`,
  `ha_dr.py`) — `docs/extension-model-unification-plan-phase2.md`.
- **`FIX_REGISTRY`'s fate** — decided in Task 0 below (a documentation fix,
  not a code change): it stays exactly as-is. It is not part of this
  migration's scope because it resolves a completely different axis
  (remediation **skill selection** during onboarding/fix) from what this
  plan and the spec address (assessment-side **detection** redundancy
  between checks and analyzers). See Task 0 for the full reasoning.
- **The Python-agents-vs-skills question** (`agents/cost.py`,
  `agents/dependency.py`, `agents/codechange.py` vs. `skills/*.md`) — this is
  **not** open work needing a plan. `docs/agent-removal-readiness.md`
  (dated 2026-07-12/13, itself an audit, not a design spec) already
  resolved it: `codechange` stays Python permanently (patches the app's own
  source tree, not skill-shaped, per that doc §3); `cost`/`dependency` keep
  their Python agents *specifically and only* for two narrative-report
  outputs (`cost-report.md`, `dependency-report.md`) that need
  runtime-computed data (detected ecosystems, computed cost tier) a static
  skill template has no access to — an explicitly accepted, documented gap,
  not an oversight; every other artifact those two agents used to produce is
  already skill-covered and already gated off at the domain level by
  `agents/orchestrator.py`'s existing skip logic. `agents/capabilities.py`'s
  `AGENT_CLASSES` (re-verified during this pass, 2026-07-17) confirms only
  these 3 agent classes exist today — `hardening.py`, `observability.py`,
  `cicd.py`, `compliance.py`, `infrastructure.py`, `incident.py`,
  `release.py`, `retirement.py`, and `chaos.py` agents have *already been
  deleted* since that audit ran (`AGENT_CAPABILITIES`'s own header comment
  confirms this). **Correction to the brief that motivated this plan:** that
  brief named `capability-scout` as one of "at least 3" Python agents in
  this fragmentation. It is not. `capability_scout.py` (top-level
  `src/agentit/`, not `src/agentit/agents/`) is part of a completely
  unrelated system — AgentIT's own self-improvement watcher, which proposes
  changes to AgentIT's *own* repository (see `docs/self-improvement-for-agentit.md`)
  — and has nothing to do with assessment/remediation for onboarded apps.
  There is no pending engineering work on the agents-vs-skills axis; nothing
  in this plan or Plan 2 touches `agents/`.

## Task 0 — Document `FIX_REGISTRY` as the spec's missing 5th system (no code change)

**Why this is a real gap, verified:** `docs/extension-model-unification-spec.md`
§0's table inventories exactly 4 systems (Analyzers, Checks, Skills, Python
agents) as "what actually exists today." `src/agentit/remediation/registry.py`'s
`FIX_REGISTRY` (a `dict[str, tuple[str, str]]` mapping a finding `category`
to a `(domain, skill_name)` pair) is real, already fully integrated, and
already tested — `skill_engine.py`'s `SkillEngine.skill_for_category()`
(lines 472-513) documents its own resolution order in its docstring
("`FIX_REGISTRY` is now authoritative... falls back to keyword-trigger
matching" for anything the registry doesn't cover), and
`tests/test_skill_registry_agreement.py` already asserts, for every one of
`FIX_REGISTRY`'s 14 category keys, that `skill_for_category()` resolves to
the exact skill the registry names (`TestRegistryAndSkillEngineAgree`) and
that `RemediationDispatcher._dispatch_generate` routes through that same
function (`TestDispatcherRoutesThroughSameFunction`). None of this is
broken, ambiguous, or in need of a code change — it is simply **absent from
the spec's own accounting of how many systems exist**, which is the concrete
gap this task closes.

**Recommendation on `FIX_REGISTRY`'s fate: leave it exactly as it is,
permanently — do not fold it into this unification.** Reasoning:

1. It operates on a different axis than everything this plan (and Plan 2)
   touches. Checks vs. analyzers (this plan's whole subject) is a
   **detection-side** redundancy — two systems independently scoring the
   same repo during assessment. `FIX_REGISTRY` is a **remediation-side
   selection** mechanism — given a finding category already produced by
   assessment, which one skill should generate the fix. There is no
   analogous redundancy to remove: nothing else in this codebase also maps
   category → skill in a way that duplicates `FIX_REGISTRY`'s job (the
   keyword-trigger fallback inside `skill_for_category()` is not a second,
   competing system — it *is* `skill_for_category()`, the same function
   `FIX_REGISTRY` is a precedence layer in front of; `test_skill_registry_agreement.py`
   exists precisely to guarantee they never disagree again the way they once
   silently did — see that test file's own docstring for the exact historical
   bug: `"policy"` used to resolve to `kyverno-require-labels` via the
   registry and `image-registry-policy` via trigger-matching, alphabetically,
   silently).
2. It is small (14 entries), Python-native, and correctly so — it exists to
   resolve a small number of *known, previously-ambiguous* categories
   deterministically; the categories it does not cover correctly fall
   through to trigger-matching. Making it data-driven (e.g. a YAML file)
   would add a loader, a schema, and a test suite for zero behavioral gain
   over a 14-line dict that already has one.
3. **Skill *selection* and skill *content* are legitimately distinct
   concerns**, per the framing in the task that produced this plan — and
   `FIX_REGISTRY` is purely the former. Folding it into the checks/analyzers
   unification (or into skills' own frontmatter, e.g. adding a `triggers`
   override per skill) would conflate a remediation-routing decision with a
   detection-format decision for no benefit; the spec's own Option B
   reasoning (§2 point 1: "skills and checks solve genuinely different
   problems and conflating them adds risk for no proven benefit") applies
   here with equal force to skill-selection vs. detection.

**The fix — add a 5th row to the spec's own inventory table.** In
`docs/extension-model-unification-spec.md`, locate this exact text (§0,
lines 29-34):

```
| System | Phase | Format | Loader | Count |
|---|---|---|---|---|
| Analyzers | Assessment (detect) | Python classes, `analyze(repo_path) -> DimensionScore` | `runner.run_assessment()`'s hardcoded `analyzers = [...]` list | 7 classes (`security`, `observability`, `cicd`, `infrastructure`, `compliance`, `data_governance`, `ha_dr`) + `eol.py` (a helper `infrastructure.py` calls, not a standalone analyzer) |
| Checks | Assessment (detect) | Declarative YAML, 5 generic rule types | `check_engine.load_checks()` + `run_checks_by_dimension_with_status()`, merged into analyzer scores by `runner._merge_check_findings()` | 20 YAML files across the same 7 dimensions |
| Skills | Onboarding/fix (remediate) | Markdown + YAML frontmatter, keyword-`triggers` matched against a report's finding text | `skill_engine.SkillEngine` | 45 `.md` files across 14 domains |
| Python agents | Onboarding/fix (remediate) | Python classes, `.run() -> Result(files: list[GeneratedFile])` | `agents/orchestrator.py::FleetOrchestrator` (skill-covered domains skip the matching agent) | 3 surviving: `cost`, `dependency`, `codechange` |
```

Replace it with (new row + trailing note, table body otherwise byte-for-byte
identical):

```
| System | Phase | Format | Loader | Count |
|---|---|---|---|---|
| Analyzers | Assessment (detect) | Python classes, `analyze(repo_path) -> DimensionScore` | `runner.run_assessment()`'s hardcoded `analyzers = [...]` list | 7 classes (`security`, `observability`, `cicd`, `infrastructure`, `compliance`, `data_governance`, `ha_dr`) + `eol.py` (a helper `infrastructure.py` calls, not a standalone analyzer) |
| Checks | Assessment (detect) | Declarative YAML, 5 generic rule types | `check_engine.load_checks()` + `run_checks_by_dimension_with_status()`, merged into analyzer scores by `runner._merge_check_findings()` | 20 YAML files across the same 7 dimensions |
| Skills | Onboarding/fix (remediate) | Markdown + YAML frontmatter, keyword-`triggers` matched against a report's finding text | `skill_engine.SkillEngine` | 45 `.md` files across 14 domains |
| Python agents | Onboarding/fix (remediate) | Python classes, `.run() -> Result(files: list[GeneratedFile])` | `agents/orchestrator.py::FleetOrchestrator` (skill-covered domains skip the matching agent) | 3 surviving: `cost`, `dependency`, `codechange` |
| Fix registry | Onboarding/fix (remediate) — **skill *selection*, not detection; orthogonal to this doc's checks-vs-analyzers scope, see note below** | Static Python `dict[category, (domain, skill_name)]` | `remediation/registry.py::lookup()`, consulted first by `skill_engine.SkillEngine.skill_for_category()` before its own keyword-trigger fallback | 14 category keys |
```

**Note on the fix registry:** this system was absent from the table above in
every earlier draft of this doc — a real gap in this doc's own "what
actually exists today" accounting, not a deliberate omission. It is listed
here for completeness but is **out of scope for this doc's Option B**
(and for `docs/extension-model-unification-plan.md`/
`docs/extension-model-unification-plan-phase2.md`, the implementation plans
that followed this spec): it resolves which *skill* handles a finding
category, a remediation-selection concern, not which *system detects* a
finding, the detection-side redundancy (checks vs. analyzers) this doc is
about. See `skill_engine.py`'s `SkillEngine.skill_for_category()` docstring
and `tests/test_skill_registry_agreement.py` for how it's already integrated
and regression-tested; no change to it is recommended.

## Task 1 — Gap 1: list-pattern OR matching + case-insensitive content matching in `check_engine.py`

**Why case-insensitivity is part of Gap 1, not a separate gap:** the spec's
own §3 Gap 1 only asked for list-pattern OR matching. Reading
`analyzers/observability.py` in full (not just its `CHECKS` dict shape, the
way the spec's own §1 warns against doing for the other analyzers) surfaces
a second, real semantic gap the spec didn't name: `observability.py` builds
`all_lower = "...".lower()` before matching (`analyzers/observability.py:29`)
and every one of its 6 pattern lists is itself already lowercase (`OTEL_PATTERNS`,
`METRICS_PATTERNS`, etc.) — i.e. it deliberately matches
case-insensitively (`"OpenTelemetry"` in a Go import or a Markdown heading
still matches the lowercase pattern `"opentelemetry"`). `check_engine.py`'s
`_run_file_contains`/`_run_yaml_kind_exists` do a plain case-sensitive `in`
check today. Porting `observability.py` without also closing this gap would
silently narrow every ported finding's real-world matching (Task 3's parity
test is specifically designed to catch this if it's missed — see its
`test_mixed_case_content_identical_findings`).

### Step 1.1 — write the failing tests

Add this new test class to `tests/test_check_engine.py`, immediately after
the existing `TestYamlKindMissing` class (before the `# Dimension grouping`
section divider, i.e. after line 212):

```python
class TestListPatternMatching:
    """Gap 1 (docs/extension-model-unification-plan.md Task 1): pattern
    becomes str | list[str] with OR semantics, and an optional
    case_insensitive flag -- both additive, zero behavior change for any
    check file that doesn't use them."""

    def test_parse_check_file_accepts_list_pattern(self, tmp_path: Path) -> None:
        content = VALID_CHECK.replace('pattern: "Dockerfile*"', "pattern: [foo, bar]")
        p = _write_check(tmp_path, "listpat", content)
        defn = _parse_check_file(p)
        assert defn is not None
        assert defn.pattern == ["foo", "bar"]

    def test_parse_check_file_still_accepts_scalar_pattern(self, tmp_path: Path) -> None:
        """Regression guard: every existing check file (scalar pattern,
        no case_insensitive key) must parse identically to before."""
        p = _write_check(tmp_path, "scalarpat", VALID_CHECK)
        defn = _parse_check_file(p)
        assert defn is not None
        assert defn.pattern == "Dockerfile*"
        assert defn.case_insensitive is False

    def test_file_contains_matches_any_pattern_in_list(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.go": 'import "go.opentelemetry.io/otel"\n'})
        content = VALID_CHECK.replace("type: file_exists", "type: file_contains").replace(
            'pattern: "Dockerfile*"', "pattern: [opentelemetry, otel, jaeger]"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_file_contains_fails_when_no_pattern_in_list_matches(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.go": "package main\n"})
        content = VALID_CHECK.replace("type: file_exists", "type: file_contains").replace(
            'pattern: "Dockerfile*"', "pattern: [opentelemetry, otel, jaeger]"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1

    def test_yaml_kind_exists_matches_any_kind_in_list(self, create_mock_repo) -> None:
        repo = create_mock_repo({"svc.yaml": "apiVersion: v1\nkind: PodMonitor\n"})
        content = VALID_CHECK.replace("type: file_exists", "type: yaml_kind_exists").replace(
            'pattern: "Dockerfile*"', "pattern: [ServiceMonitor, PodMonitor]"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_file_exists_matches_any_glob_in_list(self, create_mock_repo) -> None:
        repo = create_mock_repo({"Containerfile": "FROM ubi9"})
        content = VALID_CHECK.replace('pattern: "Dockerfile*"', "pattern: [Dockerfile*, Containerfile*]")
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_case_insensitive_file_contains(self, create_mock_repo) -> None:
        repo = create_mock_repo({"README.md": "# Structlog Setup Guide\n"})
        content = (
            VALID_CHECK.replace("type: file_exists", "type: file_contains")
            .replace('pattern: "Dockerfile*"', "pattern: structlog")
            + "case_insensitive: true\n"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0

    def test_case_sensitive_by_default(self, create_mock_repo) -> None:
        """No case_insensitive key -> exact-case matching, unchanged from
        today. This is the zero-behavior-change guarantee for all 20
        pre-existing check files."""
        repo = create_mock_repo({"README.md": "# Structlog Setup Guide\n"})
        content = VALID_CHECK.replace("type: file_exists", "type: file_contains").replace(
            'pattern: "Dockerfile*"', "pattern: structlog"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 1  # "Structlog" (capital S) != "structlog"

    def test_case_insensitive_yaml_kind_exists(self, create_mock_repo) -> None:
        repo = create_mock_repo({"svc.yaml": "apiVersion: v1\nkind: servicemonitor\n"})
        content = (
            VALID_CHECK.replace("type: file_exists", "type: yaml_kind_exists")
            .replace('pattern: "Dockerfile*"', "pattern: ServiceMonitor")
            + "case_insensitive: true\n"
        )
        checks = load_checks(_dir_with_check(repo.parent, content))
        findings = run_checks(checks, repo)
        assert len(findings) == 0
```

### Step 1.2 — run it, confirm it fails

```bash
cd AgentIT-ui-redesign
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/test_check_engine.py -k TestListPatternMatching -v
```

Expected: `test_parse_check_file_accepts_list_pattern` fails with
`AttributeError`/assertion mismatch (`str(data["pattern"])` on a list
produces the Python `repr` of the list, not `["foo", "bar"]`);
`test_case_insensitive_file_contains` fails (finding still fires, since
`"structlog" in "# Structlog Setup Guide\n"` is `False`); the others fail
with `_parse_check_file` returning a check whose `.pattern` is a stringified
list rather than a real list, so `_run_file_contains`'s `check.pattern in
content` never matches a comma-joined literal string. Confirm every test in
the new class fails or errors before proceeding — do not skip this
verification step.

### Step 1.3 — minimal implementation

In `src/agentit/check_engine.py`:

Replace lines 34-61 (the `CheckDefinition` class) with:

```python
class CheckDefinition:
    """A single parsed check loaded from a YAML file."""

    __slots__ = ("name", "dimension", "severity", "category", "check_type", "pattern",
                 "description", "recommendation", "source_path", "case_insensitive")

    def __init__(
        self,
        name: str,
        dimension: str,
        severity: Severity,
        category: str,
        check_type: str,
        pattern: str | list[str],
        description: str,
        recommendation: str,
        source_path: str,
        case_insensitive: bool = False,
    ) -> None:
        self.name = name
        self.dimension = dimension
        self.severity = severity
        self.category = category
        self.check_type = check_type
        self.pattern = pattern
        self.description = description
        self.recommendation = recommendation
        self.source_path = source_path
        self.case_insensitive = case_insensitive
```

Replace lines 91-101 (the `return CheckDefinition(...)` at the end of
`_parse_check_file`) with:

```python
    raw_pattern = data["pattern"]
    pattern: str | list[str] = (
        [str(p) for p in raw_pattern] if isinstance(raw_pattern, list) else str(raw_pattern)
    )

    return CheckDefinition(
        name=data["name"],
        dimension=data["dimension"],
        severity=sev,
        category=data["category"],
        check_type=check_type,
        pattern=pattern,
        description=data["description"],
        recommendation=data["recommendation"],
        source_path=str(path),
        case_insensitive=bool(data.get("case_insensitive", False)),
    )
```

Replace lines 120-168 (the five `_run_*` runner functions) with:

```python
def _pattern_list(pattern: str | list[str]) -> list[str]:
    """Normalize a ``CheckDefinition.pattern`` (scalar or list) into a list
    so every runner applies OR semantics the same way."""
    return pattern if isinstance(pattern, list) else [pattern]


def _run_file_exists(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if at least one file matches any glob in *pattern*."""
    patterns = _pattern_list(check.pattern)
    for fp in repo_path.rglob("*"):
        if fp.is_file() and not is_ignored(fp, repo_path):
            if any(fnmatch.fnmatch(fp.name, p) for p in patterns):
                return None
    return _make_finding(check)


def _run_file_contains(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if any non-ignored text file contains any
    pattern in *pattern* (case-insensitively if ``check.case_insensitive``)."""
    patterns = _pattern_list(check.pattern)
    if check.case_insensitive:
        patterns = [p.lower() for p in patterns]
    for fp in repo_path.rglob("*"):
        if not fp.is_file() or is_ignored(fp, repo_path):
            continue
        try:
            content = fp.read_text(errors="ignore")
        except OSError:
            continue
        haystack = content.lower() if check.case_insensitive else content
        if any(p in haystack for p in patterns):
            return None
    return _make_finding(check)


def _run_file_missing(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return a finding if a file matching any glob in *pattern* IS found."""
    patterns = _pattern_list(check.pattern)
    for fp in repo_path.rglob("*"):
        if fp.is_file() and not is_ignored(fp, repo_path):
            if any(fnmatch.fnmatch(fp.name, p) for p in patterns):
                return _make_finding(check, file_path=str(fp.relative_to(repo_path)))
    return None


def _run_yaml_kind_exists(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return ``None`` (pass) if any YAML file contains ``kind: <p>`` for
    any p in *pattern* (case-insensitively if ``check.case_insensitive``)."""
    patterns = _pattern_list(check.pattern)
    needles = [f"kind: {p}" for p in patterns]
    if check.case_insensitive:
        needles = [n.lower() for n in needles]
    for _, content in iter_yaml_files(repo_path):
        haystack = content.lower() if check.case_insensitive else content
        if any(n in haystack for n in needles):
            return None
    return _make_finding(check)


def _run_yaml_kind_missing(check: CheckDefinition, repo_path: Path) -> Finding | None:
    """Return a finding if any YAML file contains ``kind: <p>`` for any p
    in *pattern* (case-insensitively if ``check.case_insensitive``)."""
    patterns = _pattern_list(check.pattern)
    needles = [f"kind: {p}" for p in patterns]
    if check.case_insensitive:
        needles = [n.lower() for n in needles]
    for path, content in iter_yaml_files(repo_path):
        haystack = content.lower() if check.case_insensitive else content
        if any(n in haystack for n in needles):
            return _make_finding(check, file_path=str(path.relative_to(repo_path)))
    return None
```

No other lines in `check_engine.py` change — `VALID_TYPES`, `SEVERITY_MAP`,
`load_checks`, `_make_finding`, `_RUNNERS`, and every public function below
line 191 are untouched.

### Step 1.4 — run it, confirm it passes

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/test_check_engine.py -v
```

Expected: every test in `TestListPatternMatching` passes, and every
pre-existing class in this file (`TestParseCheckFile`, `TestLoadChecks`,
`TestFileExists`, `TestFileContains`, `TestFileMissing`, `TestYamlKindExists`,
`TestYamlKindMissing`, `TestRunChecksByDimension`, `TestRunChecksWithStatus`,
`TestRunnerIntegration`, `TestAdmissionPoliciesCheck`, `TestSampleAppFixture`)
still passes unchanged — these exercise only scalar patterns with no
`case_insensitive` key, which is exactly the zero-behavior-change path.

### Step 1.5 — commit

```bash
git add src/agentit/check_engine.py tests/test_check_engine.py
git commit -m "check_engine: add list-pattern OR matching + case-insensitive content matching (Gap 1)"
```

## Task 2 — port `analyzers/observability.py`'s 6 categories to `checks/observability/*.yaml`

**Exact source of truth for every pattern/description/recommendation/severity
below**: `src/agentit/analyzers/observability.py:8-22` (`OTEL_PATTERNS`
through `ALERTING_PATTERNS`, the `CHECKS` dict, and the severity rule —
`Severity.high` for `instrumentation`/`metrics`, `Severity.medium` for the
other 4). `checks/observability/health-check.yaml` is **not** one of these 6
— it has no analyzer equivalent (`observability.py`'s `CHECKS` dict has no
`"health"` key) and is left completely untouched by this task.

Current state of `checks/observability/` (verified 2026-07-17): 3 files.
`health-check.yaml` — unrelated, untouched. `metrics-endpoint.yaml` and
`structured-logging.yaml` each cover exactly 1 of their category's real
pattern set (`ServiceMonitor` only; `structlog` only) vs. the analyzer's 5
and 6 respectively — both need the Task 1 list-pattern upgrade.
`instrumentation`, `tracing`, `dashboards`, `alerting` have **no check file
at all** today (this directly confirms the claim in the brief that produced
this plan: 3 files exist, not the 6 the spec's own Phase 1 calls for).

### Step 2.1 — write the 4 new check files

Create `checks/observability/instrumentation.yaml`:

```yaml
name: otel-instrumentation-detected
dimension: observability
severity: high
category: instrumentation
type: file_contains
pattern: [opentelemetry, otel, go.opentelemetry.io, io.opentelemetry, "@opentelemetry"]
case_insensitive: true
description: No OpenTelemetry or metrics instrumentation detected
recommendation: Add OpenTelemetry SDK for auto-instrumentation
```

Create `checks/observability/tracing.yaml`:

```yaml
name: distributed-tracing-detected
dimension: observability
severity: medium
category: tracing
type: file_contains
pattern: [jaeger, zipkin, tempo, trace, opentracing]
case_insensitive: true
description: No distributed tracing detected
recommendation: Add OpenTelemetry tracing with Tempo exporter
```

Create `checks/observability/dashboards.yaml`:

```yaml
name: grafana-dashboards-detected
dimension: observability
severity: medium
category: dashboards
type: file_contains
pattern: [grafana, dashboard]
case_insensitive: true
description: No Grafana dashboards found
recommendation: Create Grafana dashboards for RED metrics
```

Create `checks/observability/alerting.yaml`:

```yaml
name: alerting-rules-detected
dimension: observability
severity: medium
category: alerting
type: file_contains
pattern: [prometheusrule, alertmanager, alerting, pagerduty, opsgenie]
case_insensitive: true
description: No alerting rules or integrations found
recommendation: Define PrometheusRule alerting rules for SLO-based alerts
```

### Step 2.2 — upgrade the 2 existing partial-coverage files

Replace the full content of `checks/observability/metrics-endpoint.yaml`
(currently `type: yaml_kind_exists`, `pattern: ServiceMonitor`, single
pattern) with:

```yaml
name: prometheus-metrics-exists
dimension: observability
severity: high
category: metrics
type: file_contains
pattern: [prometheus, servicemonitor, podmonitor, metrics, statsd]
case_insensitive: true
description: No Prometheus metrics or ServiceMonitor found
recommendation: Create ServiceMonitor for Prometheus scraping
```

(`type` changes from `yaml_kind_exists` to `file_contains` — the analyzer's
real condition is "any of these 5 substrings appears anywhere in the repo,"
not "a YAML file declares `kind: ServiceMonitor`"; the old `yaml_kind_exists`
version was itself a narrower approximation, not the actual ported analyzer
finding.)

Replace the full content of `checks/observability/structured-logging.yaml`
(currently `pattern: structlog`, single pattern, no `case_insensitive`) with:

```yaml
name: structured-logging-detected
dimension: observability
severity: medium
category: logging
type: file_contains
pattern: [structlog, zap, logrus, winston, pino, slog]
case_insensitive: true
description: No structured logging library detected
recommendation: Add structured JSON logging (e.g., structlog for Python, zap for Go)
```

### Step 2.3 — run the schema regression suite

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/test_all_checks.py -v
```

Expected: all pass (the 6 files above satisfy `test_all_checks.py`'s
`REQUIRED_FIELDS`/`VALID_SEVERITIES`/`VALID_TYPES` checks unchanged — no
edit to `test_all_checks.py` is needed in this plan; `case_insensitive` is
an *additional*, not required, key, so `REQUIRED_FIELDS - set(data.keys())`
is still empty for every file).

### Step 2.4 — commit

```bash
git add checks/observability/
git commit -m "checks/observability: port all 6 analyzer categories using Gap 1's list-pattern matching"
```

## Task 3 — parity test: prove the 6 checks match the analyzer exactly, before deleting anything

### Step 3.1 — write the failing test

Add this class to `tests/test_observability.py`, above the two existing
top-level test functions:

```python
from pathlib import Path

from agentit.check_engine import load_checks, run_checks


class TestObservabilityCheckAnalyzerParity:
    """Phase 1 cutover proof (docs/extension-model-unification-plan.md
    Task 3): every checks/observability/*.yaml file except health-check.yaml
    (no analyzer equivalent) must produce exactly the same
    (category, description) findings as analyzers/observability.py for the
    same repo content -- required before that analyzer can be deleted."""

    OBSERVABILITY_CHECKS_DIR = Path(__file__).resolve().parent.parent / "checks" / "observability"

    def _run_checks(self, repo: Path) -> set[tuple[str, str]]:
        checks = [c for c in load_checks(self.OBSERVABILITY_CHECKS_DIR) if c.category != "health"]
        findings = run_checks(checks, repo)
        return {(f.category, f.description) for f in findings}

    def _run_analyzer(self, repo: Path) -> set[tuple[str, str]]:
        score = ObservabilityAnalyzer().analyze(repo)
        return {(f.category, f.description) for f in score.findings}

    def test_empty_repo_identical_findings(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": "print('hi')"})
        assert self._run_checks(repo) == self._run_analyzer(repo)

    def test_fully_instrumented_repo_identical_findings(self, create_mock_repo) -> None:
        repo = create_mock_repo({
            "main.go": 'import "go.opentelemetry.io/otel"\n',
            "deploy/servicemonitor.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\n",
            "deploy/grafana-dashboard.json": '{"dashboard": {}}',
            "deploy/alerting-rules.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n",
            "app.py": "import structlog\n",
            "trace.py": "from jaeger_client import Config\n",
        })
        assert self._run_checks(repo) == self._run_analyzer(repo)

    def test_partially_instrumented_repo_identical_findings(self, create_mock_repo) -> None:
        repo = create_mock_repo({"main.py": "import structlog\n"})
        assert self._run_checks(repo) == self._run_analyzer(repo)

    def test_mixed_case_content_identical_findings(self, create_mock_repo) -> None:
        """Proves case-insensitivity parity. The analyzer lowercases
        content before matching (analyzers/observability.py:29); a
        case-sensitive-only check port would diverge on exactly this
        fixture (fails before Task 1/Step 2.2's case_insensitive additions,
        passes after). Deliberately a .py file, not .md -- see the note
        immediately below this class for a second, unrelated divergence a
        Markdown fixture would trip over instead."""
        repo = create_mock_repo({"setup_notes.py": "# OpenTelemetry + Structlog Setup\n"})
        assert self._run_checks(repo) == self._run_analyzer(repo)
```

**A second, real divergence found while designing this test, deliberately
not fixed here:** `check_engine.py`'s `_run_file_contains` scans every file
in the repo (`repo_path.rglob("*")`, no extension filter). The analyzer's
`iter_text_files` (`analyzers/base.py:40-55`) restricts to a fixed
`TEXT_EXTENSIONS` allowlist (`.py`, `.go`, `.yaml`, `.json`, ... — 20
extensions, notably **not** including `.md`, `.txt`, or several others).
Confirmed empirically during this plan's own verification: a fixture with
the exact content above but in a `README.md` instead of a `.py` file
produces a real mismatch — the check (scanning the `.md` file too) sees
`"opentelemetry"`/`"structlog"` and passes, while the analyzer (which never
even reads `.md` files) still fires both findings. **This plan does not
close this gap.** Reasoning: it only ever makes the check *more lenient*
than the analyzer (finds evidence in more file types, never fewer), and
`observability.py`'s own keyword-matching was already the loosest,
least-guarded kind of evidence this codebase's analyzers use (a bare
substring anywhere in covered source, no co-occurrence or context checks) —
extending that same philosophy to documentation files is consistent with,
not a departure from, the analyzer's own original design. This is a
deliberately different call than Plan 2 makes for
`data_governance.py`'s "backup" finding (Plan 2, Task 5), where the
analyzer's author *did* add an explicit precision guard (a same-file
co-occurrence requirement) specifically to avoid a bare, unrelated
substring match — discarding that guard there would be a real accuracy
regression in a way that widening observability's file-type net is not.
Restricting `check_engine.py`'s file-scanning to `TEXT_EXTENSIONS` globally
would fix this but is out of scope here: it would change every one of the
20 pre-existing check files' real-world matching behavior, a much larger
blast radius than this plan's observability-only scope, and no evidence
gathered during this plan suggests it is currently causing a problem for
any of them.

### Step 3.2 — run it, confirm it passes now (it should, since Tasks 1-2 already landed)

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/test_observability.py -k Parity -v
```

If any of these 4 fail, do **not** proceed to Task 4/5 — the mismatch is a
real divergence between the ported checks and the analyzer that must be
fixed in the relevant `checks/observability/*.yaml` file (most likely a
missed pattern or a missing `case_insensitive: true`) before any analyzer
code is deleted. This is the concrete instance of the spec's own §5 Phase 1
instruction: "run both... side-by-side against a real repo fixture, assert
identical findings, *then* delete."

### Step 3.3 — commit

```bash
git add tests/test_observability.py
git commit -m "tests: prove checks/observability/*.yaml has exact finding parity with the analyzer"
```

## Task 4 — fix the "checks-only dimension silently vanishes when clean" gap

**Why this must land before Task 5, not after:** `runner._merge_check_findings`
today only adds a dimension to the merged `score_map` when at least one
check for that dimension produced a `Finding` (`extra.items()` only contains
dimensions with ≥1 failing check — `run_checks_by_dimension_with_status`'s
`grouped.setdefault(...)` is only called inside `if finding is not None`).
While every dimension still has an analyzer, this is invisible: the analyzer
unconditionally returns a `DimensionScore` (score 100, empty findings) even
when it finds nothing wrong. The moment observability's *only* remaining
producer is checks (Task 5), a repo with a perfect observability score would
get **zero** `DimensionScore` rows for `"observability"` at all — not a
`100/100` row, no row — because every one of its 7 checks passed and
`extra` never gets an `"observability"` key. This is a real, currently
latent regression this plan must close before the cutover, not an
inherited, already-covered concern the spec's §4 table's "unchanged"
verdict on `_merge_check_findings` actually accounted for (that table's
claim is about the dedup logic specifically, not this initial-population gap).

### Step 4.1 — write the failing test

Add to `tests/test_check_engine.py`'s `TestRunnerIntegration` class
(after `test_empty_checks_dir_no_change`, i.e. after line 361):

```python
    def test_checks_only_dimension_appears_with_clean_score_when_all_checks_pass(
        self, create_mock_repo, tmp_path: Path,
    ) -> None:
        """A dimension whose only producer is checks (no analyzer) must
        still appear in report.scores with a clean 100/100 score when every
        one of its checks passes -- not silently vanish. Uses a synthetic
        dimension name so this test is valid whether or not any real
        analyzer has been ported yet."""
        repo = create_mock_repo({"main.py": "print('hi')"})
        checks_dir = tmp_path / "checks_only_dim"
        checks_dir.mkdir()
        (checks_dir / "clean.yaml").write_text(
            "name: always-passes\n"
            "dimension: totally_synthetic_dimension\n"
            "severity: low\n"
            "category: synthetic\n"
            "type: file_missing\n"
            'pattern: "this-file-should-never-exist.xyz"\n'
            "description: synthetic finding that should never fire\n"
            "recommendation: n/a\n"
        )
        from agentit.runner import run_assessment
        report = run_assessment(
            repo, repo_url="https://github.com/test/app", checks_dir=checks_dir,
        )
        synthetic = next(
            (s for s in report.scores if s.dimension == "totally_synthetic_dimension"), None,
        )
        assert synthetic is not None, "checks-only dimension vanished when its only check passed"
        assert synthetic.score == 100
        assert synthetic.findings == []
```

### Step 4.2 — run it, confirm it fails

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/test_check_engine.py -k clean_score -v
```

Expected: `AssertionError: checks-only dimension vanished when its only
check passed` (`synthetic` is `None`).

### Step 4.3 — minimal implementation

In `src/agentit/runner.py`, replace the body of `_merge_check_findings`
(lines 124-173) with:

```python
def _merge_check_findings(
    scores: list[DimensionScore],
    check_defs: list,
    repo_path: Path,
) -> tuple[list[DimensionScore], list[dict]]:
    """Merge data-driven check findings into existing analyzer scores.

    New findings from checks supplement (don't replace) analyzer findings.
    Findings are deduplicated by (category, description) so overlapping
    checks don't double-count. Every dimension covered by *any* check gets
    a DimensionScore row -- even one with zero failing checks (a clean
    100/100), exactly like an analyzer already always does -- so a
    dimension whose only producer is checks never silently disappears from
    report.scores just because every one of its checks passed. Returns the
    merged scores plus a pass/fail status row for every check that ran (for
    `check_results_out`).
    """
    extra, check_statuses = run_checks_by_dimension_with_status(check_defs, repo_path)

    score_map = {s.dimension: s for s in scores}
    original_dims = {s.dimension for s in scores}

    for dimension, findings in extra.items():
        existing = score_map.get(dimension)
        if existing is not None:
            existing_keys = {
                (f.category, f.description) for f in existing.findings
            }
            new_findings = [
                f for f in findings
                if (f.category, f.description) not in existing_keys
            ]
            if new_findings:
                merged = existing.findings + new_findings
                score_map[dimension] = DimensionScore(
                    dimension=dimension,
                    score=calculate_score(merged),
                    max_score=existing.max_score,
                    findings=merged,
                )
        else:
            # Dimension from checks not covered by any analyzer
            score_map[dimension] = DimensionScore(
                dimension=dimension,
                score=calculate_score(findings),
                max_score=100,
                findings=findings,
            )

    checked_dims = {s["dimension"] for s in check_statuses}
    for dimension in checked_dims:
        if dimension not in score_map:
            score_map[dimension] = DimensionScore(
                dimension=dimension, score=100, max_score=100, findings=[],
            )

    merged_scores = [score_map[s.dimension] for s in scores] + [
        score_map[d] for d in score_map if d not in original_dims
    ]
    return merged_scores, check_statuses
```

The only structural change from today's version: the `if not extra: return
scores, check_statuses` early return is removed (it is exactly what causes
the bug — it exits before the new `checked_dims` loop ever runs), and the
new `checked_dims` loop is added after the existing merge loop, before the
final `merged_scores` assembly (which is otherwise byte-for-byte unchanged).

### Step 4.4 — run it, confirm it passes, then run the full existing suite for this file

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/test_check_engine.py tests/test_runner.py -v
```

Expected: the new test passes; every pre-existing test in both files still
passes — in particular `test_empty_checks_dir_no_change` (checks_dir has no
files → `check_defs == []` → `run_assessment`'s `if check_defs:` guard means
`_merge_check_findings` is never even called, this task's change is
unreachable code for that test) and `test_duplicate_findings_not_doubled`/
`test_check_findings_merged_into_scores` (both exercise dimensions that
already have an analyzer entry in `scores`, so the new `checked_dims` loop's
`if dimension not in score_map` guard is a no-op for them).

### Step 4.5 — commit

```bash
git add src/agentit/runner.py tests/test_check_engine.py
git commit -m "runner: fix checks-only dimensions silently vanishing from report.scores when clean"
```

## Task 5 — cutover: delete `analyzers/observability.py`

### Step 5.1 — remove the analyzer from `runner.py`

In `src/agentit/runner.py`, delete line 12
(`from agentit.analyzers.observability import ObservabilityAnalyzer`) and,
inside `run_assessment`'s `analyzers = [...]` list (lines 74-82), delete the
line `ObservabilityAnalyzer(),`. `AGENT_MAP`'s `"observability": "Observability
Bootstrap Agent"` entry (line 22) is **unchanged** — it is keyed by
dimension name, not by analyzer-vs-check origin, and stays correct regardless
of which system now produces that dimension's findings (confirms the spec's
own §4 table claim for this row).

### Step 5.2 — delete the analyzer file

```bash
git rm src/agentit/analyzers/observability.py
```

### Step 5.3 — replace `tests/test_observability.py`'s content

The file's existing 2 top-level tests import `ObservabilityAnalyzer`
directly (line 1) — that import now fails. Replace the entire file content
with (the parity test class from Task 3 is deleted here too — its whole
purpose was proving equivalence *before* the analyzer existed to compare
against; once the analyzer is gone there is nothing left to compare):

```python
"""Tests for the observability dimension, sourced entirely from
checks/observability/*.yaml since analyzers/observability.py was retired
(see docs/extension-model-unification-plan.md, Phase 1 cutover)."""

from agentit.runner import run_assessment


def test_no_observability_scores_low(create_mock_repo):
    repo = create_mock_repo({"main.go": "package main\nfunc main() {}\n"})
    report = run_assessment(repo, repo_url="https://github.com/test/app")
    obs = next(s for s in report.scores if s.dimension == "observability")
    assert obs.score <= 20


def test_full_observability_scores_high(create_mock_repo):
    repo = create_mock_repo({
        "main.go": 'import "go.opentelemetry.io/otel"\n',
        "deploy/servicemonitor.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\n",
        "deploy/grafana-dashboard.json": '{"dashboard": {}}',
        "deploy/alerting-rules.yaml": "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n",
        "app.py": "import structlog\n",
        "trace.py": "from jaeger_client import Config\n",
        "deploy/deployment.yaml": "livenessProbe:\n  httpGet:\n    path: /health\n",
    })
    report = run_assessment(repo, repo_url="https://github.com/test/app")
    obs = next(s for s in report.scores if s.dimension == "observability")
    assert obs.score >= 60
```

### Step 5.4 — run the full affected test surface, Step 5.5 — full regression run, Step 5.6 — commit

```bash
KUBECONFIG=/tmp/nonexistent-path python -m pytest \
  tests/test_check_engine.py tests/test_observability.py tests/test_runner.py \
  tests/test_all_checks.py tests/test_skill_agent_parity.py -v
KUBECONFIG=/tmp/nonexistent-path python -m pytest tests/ -x -q
git add src/agentit/runner.py tests/test_observability.py
git rm src/agentit/analyzers/observability.py
git commit -m "Retire analyzers/observability.py -- checks/observability/*.yaml is now the sole producer"
```

## Task 6 — update this plan's own status header (repo doc convention)

Once Tasks 0-5 are all committed and Step 5.5's full-suite run is clean,
edit this file's own top "Status" line, following the exact in-place-update
convention `docs/kafka-hardening-plan.md`/`docs/postgres-migration-plan.md`
use (append context, never delete the original body).

## Self-review (from the original doc)

**Every requirement in the spec's Phase 0 + Phase 1 (§5) is mapped to a
task above:** Phase 0 ("Gap 1 only, no analyzer touched yet... Gap 2 is
deliberately not built in this phase") → Task 1 (Gap 2/`all_of` is untouched
here, confirmed deferred to Plan 2). Phase 1 ("port observability.py only,
end to end... write checks/observability/*.yaml for all 6... run both...
side-by-side... assert identical findings, then delete... in the same
commit") → Tasks 2 (write the 6), 3 (prove identical findings), 5 (delete +
same-commit `runner.py` list edit).

**Deliberately excluded from this plan, and why:** the 4 mixed analyzers
(Plan 2, separate document — too large/uncertain for one plan, see "Why two
plans" above); `FIX_REGISTRY` code changes (Task 0's conclusion: it's
correctly out of scope, a documentation-only fix instead); the
Python-agents-vs-skills question (already resolved by
`docs/agent-removal-readiness.md`, not open work at the time this plan was
written — **now reopened by the user's 2026-07-18 direction**, see the
rescue note at the top of this file).
