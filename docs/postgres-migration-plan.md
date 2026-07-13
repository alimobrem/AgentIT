# Postgres Migration Plan (deferred work)

**Status: Phase 1 (schema) and Phase 2 (async store rewrite) are done and verified against a real Postgres. Phase 3 is partially done: `cli.py` and the 4 watchers are converted; `dispatcher.py`/`remediation_loop.py` are a documented blocker (see the "Progress update: Phase 3" section below); the portal (`app.py`/`routes/*.py`/`helpers.py`) and Phase 4 (drop SQLite) are not started.** See that section for exactly what landed, what's blocked and why, and what's left — the rest of this document is the original planning doc and is still accurate for the remaining phases.

Tonight's original prep work was strictly HA Postgres chart plumbing (`chart/templates/postgres/`), the `asyncpg` dependency addition, and this plan — `store.py` and every one of its callers are untouched and unaffected. This doc exists so the actual rewrite (a separate, later effort) has an accurate map instead of starting from a cold read of a 1,400-line file.

## Progress update: Phase 1 + Phase 2 complete

Implemented in `src/agentit/portal/store_pg.py`, additively — **nothing in `src/agentit` imports this module yet**, so `store.py` and all ~15 callers listed in §1 remain completely untouched. This was deliberate: it lets this land immediately with zero merge/contention risk (the exact concern that deferred this work originally, per the top of this doc), while doing the two most self-contained, highest-value phases now.

- **Phase 1 (schema):** `store_pg.SCHEMA_SQL` — idempotent DDL for all 16 tables, translated table-for-table from `store.py`'s inline `CREATE TABLE` statements using the type mapping in §4 below (`TIMESTAMPTZ` for every timestamp column, `JSONB` for every JSON-blob column, `BOOLEAN` for `dry_run`/`enabled`, `GENERATED ALWAYS AS IDENTITY` for the two autoincrement PKs). Run once via a single multi-statement `pool.execute()` call at `AssessmentStore.create()` time — hand-rolled rather than `alembic`, matching `store.py`'s own existing convention of embedding DDL directly rather than a separate migration tool (this was an explicit either/or in the original §9 Phase 1 text; the hand-rolled option was chosen for consistency).
- **Phase 2 (async rewrite):** Every one of `store.py`'s ~90 public methods has an `async def` counterpart in `store_pg.AssessmentStore`, same method names and parameter/return shapes (per §9 Phase 2's explicit goal, so Phase 3's call-site edits stay mechanical). All `?` placeholders became `$1`/`$2`/…, `INSERT OR REPLACE` became `ON CONFLICT ... DO UPDATE`, `INSERT OR IGNORE` became `ON CONFLICT ... DO NOTHING`, and `sqlite3.Row`/`dict(row)` became `asyncpg.Record`/`dict(record)` — all exactly as anticipated in §4. `AssessmentStore.__init__` takes an already-created pool; use the `async def create(dsn, min_size=..., max_size=...)` classmethod (pool creation is async), per §9's note.
- **Two deliberate, documented shape differences from `store.py`** (both called out in `store_pg.py`'s module docstring, so Phase 3 implementers aren't surprised):
  - `asyncpg` returns `TIMESTAMPTZ` columns as `datetime` objects, not the ISO-8601 strings SQLite's `TEXT` columns returned. A `_row_to_dict` helper converts any `datetime` value back to `.isoformat()` on the way out of every dict-returning method, so callers see the exact same shape as `store.py` — this was **not** discussed as a risk in the original §4/§8 text and is worth flagging explicitly for whoever does Phase 3.
  - `dry_run`/`enabled` come back as real `bool` (Postgres `BOOLEAN`) instead of SQLite's `0`/`1` `int`. Left as-is since it's a strict improvement and every real consumer (JSON serialization, Jinja `{% if %}`) treats both as truthy/falsy identically — flagged here in case a caller ever does something like `dry_run == 1`.
- **One real portability bug found and fixed during testing:** `purge_old_data`'s "keep only the latest `onboarding_results` row per `assessment_id`" query used `GROUP BY assessment_id` while selecting the ungrouped `id` column and filtering with `HAVING created_at = MAX(created_at))` — SQLite allows this (its lenient "bare column" GROUP BY behavior), Postgres correctly rejects it (`GroupingError`). Fixed with `SELECT DISTINCT ON (assessment_id) id ... ORDER BY assessment_id, created_at DESC` instead. This is exactly the kind of thing §4's type-mapping table doesn't cover (it's a query-shape issue, not a type issue) — worth a careful read of every multi-row query during Phase 3, not just the ones already flagged here.
- **Verification:** `tests/test_store_pg.py`, 26 tests, all passing against a real `postgres:16-alpine` container (started via `podman run` during this session; the test file also auto-starts/tears down its own throw-away container via `podman`/`docker` if `AGENTIT_TEST_PG_DSN` isn't set — see the fixture there). Gated behind a new `postgres` pytest marker + `--run-postgres-tests` flag (mirrors the existing `--live-cluster` convention in `tests/conftest.py`), so the default `pytest` run (no flags) neither requires nor touches a container. `pytest-asyncio` (§8's recommendation) was added to `pyproject.toml`'s dev deps and `uv.lock` was resynced. Full existing suite (`pytest tests/ --ignore=tests/test_store_pg.py`) re-run after these changes: 1311 passed, 87 skipped, 1 pre-existing failure unrelated to this work (`TestRBAC::test_has_cluster_rolebinding` — caused by a concurrent, unrelated in-flight workstream touching the Helm chart's RBAC templates, confirmed via `git status` showing that workstream's uncommitted changes to `store.py`/`app.py`/chart files that this Postgres work never touched).
- **Not done, and deliberately out of scope for this pass:** the session-scoped `testcontainers` + per-test-transaction-rollback approach §8 recommends as the long-term CI shape — the hand-rolled `podman`/`docker subprocess` fixture in `tests/test_store_pg.py` was faster to stand up and didn't need a new dependency, but should be revisited if `--run-postgres-tests` becomes a routine part of CI rather than an opt-in local check.
- **Next step is Phase 3, exactly as scoped in §1/§9 below** — nothing about the call-site inventory, the suggested file order, or the "convert app + tests in lockstep" guidance has changed. Pool sizing per component should follow §5's table (the `create()` classmethod's `min_size=5, max_size=20` defaults match the Portal row specifically; each watcher's Phase 3 conversion should pass its own smaller `min_size`/`max_size` explicitly, not rely on the defaults).

## Progress update: Phase 3 (partial) -- `cli.py` + the 4 watchers converted, `dispatcher.py`/`remediation_loop.py` blocked

Converted, file-by-file with tests green at each step, per §9's suggested order -- with one addition not anticipated by the original plan (the async-SQLite shim, below) and one deliberate deviation from the suggested order (`dispatcher.py`/`remediation_loop.py` skipped, blocked -- see below), both explained here so the next implementer doesn't have to rediscover why.

**The default backend has not changed.** `AGENTIT_DB_BACKEND` is unset in every Deployment; every one of the ~15 callers in §1 (converted or not) still ends up talking to the same SQLite file at `AGENTIT_DB_PATH`/`agentit.db`. Nothing in this update flips that switch -- see §7 for why that must stay a single, deliberate, coordinated step.

### New enabling piece not in the original plan: `store_factory.py`

The plan's §9 Phase 3 text assumed callers would just start `await`ing `store.py`'s methods once it (or its Postgres counterpart) was async. In practice, `store.py` itself is deliberately untouched this pass (Phase 2's rewrite lives entirely in the separate, unwired `store_pg.py`), so callers need something to `await` *today* that (a) is behaviorally identical to calling `store.py` directly, and (b) can later point at `store_pg.AssessmentStore` via one env var without every caller changing again. That's `src/agentit/portal/store_factory.py`:

- `create_store(db_path=None, *, min_size=5, max_size=20)` -- the single factory function every converted caller now goes through. Backend selected by `AGENTIT_DB_BACKEND` (`"sqlite"` default, `"postgres"` opt-in via `store_pg.AssessmentStore.create()`).
- `AsyncSQLiteStore` -- wraps `store.py`'s synchronous `AssessmentStore` and exposes every method as an `async def` proxy through `asyncio.to_thread`. While the backend stays "sqlite" (the only thing this pass ships), this is byte-for-byte the same sqlite3 I/O, just with a thread hop -- not a behavior change.
- `.raw` -- the one piece that doesn't have a Postgres equivalent on purpose. It exposes the underlying synchronous `AssessmentStore` for the several call sites this pass explicitly does not convert (`FleetOrchestrator`, `AutoMode`, `RemediationDispatcher`, `EventConsumer`, and each watcher's own tick body -- see below). Handing one of those a Postgres-backed store instead would be exactly the silent partial-cutover §7 warns about, so `store_pg.AssessmentStore` has no `.raw` and that combination fails loudly (`AttributeError`) instead of guessing.
- Tests: `tests/test_store_factory.py` -- sqlite-backend behavior-equivalence tests run unconditionally; one `@pytest.mark.postgres` test (reusing `test_store_pg.py`'s `postgres_dsn` fixture, per §8's guidance) confirms the backend switch actually returns a `store_pg.AssessmentStore` when asked.

### `cli.py` -- converted

`_rescan_fleet` (used by `assess --rescan`/`watch --rescan`) and the `consume`, `vuln-watch`, `slo-track`, `drift-detect`, `learn-watch`, `self-assess`, `self-fix` commands are `async def`, constructing their store via `await create_store(...)`. `assess`/`watch` themselves stay plain sync Click commands -- they just `asyncio.run(_rescan_fleet(...))` for the `--rescan` branch, since their non-rescan path never touches the store at all and didn't need touching. Click has no native async command support, so a small `_run_async` decorator (`asyncio.run()` wrapping, applied as the innermost decorator) lets the rest be `async def` without adding `anyio`/`asyncclick`.

Every store handed to a not-yet-converted synchronous consumer -- `FleetOrchestrator`, `AutoMode` (both in `self-assess`), `RemediationDispatcher` (in `self-fix`), `EventConsumer` (in `consume`/`vuln-watch`/`slo-track`), and each watcher's constructor -- gets `store.raw`, not the async facade, so those code paths are 100% unaffected. Verified via the full existing `test_cli.py`/`test_cli_commands.py`/`test_watch.py` suites (unchanged, all green) plus manual `--help` smoke tests for every converted command and an explicit check that `sys.exit()` inside an `asyncio.run()`-wrapped command still produces the right `CliRunner` exit code (it does).

### The 4 watchers -- converted, tick bodies deliberately left alone

`vuln_watcher.py`, `slo_tracker.py`, `drift_detector.py`, `skill_learner.py`: only `run()` changed -- `async def run(self)`, `time.sleep(self._interval)` -> `await asyncio.sleep(self._interval)`, per §5. The tick bodies (`check_fleet`/`check_once`/`detect_once`/`research_once`) are untouched and still synchronous, because `check_fleet` and `detect_once` construct `AutoMode` (and `check_fleet` also `RemediationLoop`) and call them without `await` -- converting the tick bodies without also converting those two classes would silently break them, not just async-shape them. None of the 4 watchers previously had any test exercising `run()` at all (only the tick methods were tested); added one `TestAsyncRunLoop` class per file (`test_vuln_watcher.py` is new -- there was no test file for `VulnWatcher` before), mirroring `test_watch.py`'s `time.sleep`-raises-`KeyboardInterrupt` pattern adapted to `asyncio.sleep`.

### Blocked, not guessed at: `dispatcher.py` / `remediation_loop.py`

Per this pass's own safety rule ("if you're not confident a conversion is behavior-preserving, stop and document it as a blocker rather than guessing") -- **`RemediationDispatcher.dispatch()` and `RemediationLoop.trigger()`/`.start()` were not converted.** Both are called *synchronously, without `await`*, from `portal/app.py` and `portal/routes/webhooks.py` -- explicitly out of scope this pass (a different, concurrent workstream owns them right now). Making either method `async def` would not "async-shape" those call sites, it would silently break them: calling an `async def` method without `await` returns an un-awaited coroutine, and the very next line in both files (`result["files"]`, `result["outcome"]`) would raise `TypeError: 'coroutine' object is not subscriptable` the first time either route fires in production.

There is no safe partial move here -- `dispatcher.py`/`remediation_loop.py`'s public API is genuinely shared between an in-scope caller (`cli.py self-fix`, `vuln_watcher.check_fleet`) and out-of-scope callers (`app.py`, `webhooks.py`), and the shared methods can only have one calling convention at a time. Converting them has to happen in the *same* pass as `app.py`/`webhooks.py`, not before or after. `cli.py self-fix` and `vuln_watcher.check_fleet` still construct `RemediationDispatcher`/`RemediationLoop` exactly as before (with `store.raw`), so their behavior is unaffected -- this is purely a "not converted yet" gap, not a workaround or a regression.

### What's left for the final phase (explicitly not attempted here)

1. **The portal**: `portal/app.py`, `portal/routes/{health,schedules,webhooks}.py`, `portal/helpers.py` (which owns the `get_store()` singleton) -- all still fully synchronous, per this pass's explicit scope boundary.
2. **`dispatcher.py`/`remediation_loop.py`'s public API** -- must convert in the same pass as #1, for the reason above.
3. **`automode.py` and `agents/orchestrator.py` (`FleetOrchestrator`)** -- not in this pass's file list at all, but discovered during this work to be additional synchronous consumers that `self-assess`/`drift_detect`/`vuln_watch` hand a store into. Whoever does the final phase should add these two to the call-site inventory explicitly; they weren't listed in §1's original grep.
4. **The actual coordinated backend cutover** (§7) -- flipping `AGENTIT_DB_BACKEND=postgres` for real, across all 5 Deployments in one Argo CD sync, only after #1-3 are done and every remaining synchronous store consumer has either been converted or confirmed to have no remaining callers. Nothing in this pass changes any Deployment's env vars or the chart's `postgres.enabled` default.

## Why now, and why not tonight

`src/agentit/portal/store.py` (`AssessmentStore`) and its callers were being actively edited by multiple other parallel workstreams at the time this plan was written (unrelated bug fixes touching `store.py` itself, plus edits in `app.py`, `helpers.py`, `webhooks.py`, and `watchers/*.py`). Rewriting the store or any call site tonight would have guaranteed merge conflicts with that in-flight work. Everything below is read-only investigation (grep/inspection) plus independent, additive infrastructure (Helm chart, one `pyproject.toml` dependency line, this doc).

## 1. Call-site inventory

**Source of truth:** `AssessmentStore` is instantiated once as a module-level singleton in `src/agentit/portal/helpers.py` (`_store = AssessmentStore()`, exposed via `get_store()`), and every consumer either calls `get_store()` or receives a `store`/`self._store` reference passed into its constructor.

### Application code (`src/`) — 12 files

| File | How it touches the store | Approx. call count | Phase 3 status |
|---|---|---|---|
| `portal/store.py` | **Definition itself** — this is the file being rewritten | n/a (1,405 lines, 16 tables, ~90 public methods) | n/a |
| `portal/helpers.py` | Owns the singleton (`_store = AssessmentStore()`, `get_store()`); several direct `_store.*` calls | ~15 | ⬜ not started (out of scope this pass) |
| `portal/app.py` | `get_store()` via FastAPI `Depends`, dozens of route handlers | largest single caller — dozens of routes | ⬜ not started (out of scope this pass) |
| `portal/routes/webhooks.py` | `get_store()` via `Depends` in webhook handlers | several | ⬜ not started (out of scope this pass) |
| `portal/routes/health.py` | `get_store()` for health/status endpoints | several | ⬜ not started (out of scope this pass) |
| `portal/routes/schedules.py` | `get_store()` for CRUD on `scheduled_operations` | several | ⬜ not started (out of scope this pass) |
| `cli.py` | Constructs its store via the new `store_factory.create_store()`; commands touching the store are now `async def` | 9 constructions + calls | ✅ converted |
| `watchers/vuln_watcher.py` | `store: AssessmentStore` constructor param (unchanged type); `run()` is now `async def` | ~5 | ✅ `run()` converted; tick body (`check_fleet`) deliberately left synchronous — see progress notes |
| `watchers/slo_tracker.py` | Same pattern, SLO-table calls; `run()` is now `async def` | ~5 | ✅ `run()` converted; tick body (`check_once`) deliberately left synchronous |
| `watchers/drift_detector.py` | Same pattern, event/gate calls; `run()` is now `async def` | ~5 | ✅ `run()` converted; tick body (`detect_once`) deliberately left synchronous |
| `watchers/skill_learner.py` | Imports store types for skill inventory snapshotting; `run()` is now `async def` | few | ✅ `run()` converted; tick body (`research_once`) deliberately left synchronous |
| `remediation_loop.py` | `store: AssessmentStore` param, gate/remediation calls | ~3 | ❌ blocked — see "Progress update: Phase 3" (shared, synchronously-called API with out-of-scope `webhooks.py`) |
| `remediation/dispatcher.py` | `store: AssessmentStore` param | ~2 | ❌ blocked — see "Progress update: Phase 3" (shared, synchronously-called API with out-of-scope `app.py`/`webhooks.py`) |

Also discovered during Phase 3 (not in the original grep below, since it predates this table): **`automode.py`** and **`agents/orchestrator.py`** (`FleetOrchestrator`) are additional synchronous store consumers, reached from `cli.py self-assess`/`self-fix` and `watchers/vuln_watcher.py`/`drift_detector.py`. Add both to whatever inventory the final phase works from.

Grep totals (`rg`, repo root):

- `get_store()` call sites in `src/`: **84**
- Direct `AssessmentStore(` construction in `src/`: **9** (mostly `cli.py` and module-level singletons)
- Files under `src/` that import or reference `AssessmentStore` at all: **12** (11 callers + `store.py` itself)

### Tests (`tests/`) — 21 files

- `tests/conftest.py` defines the two central fixtures everything else builds on: `make_store()` → `AssessmentStore(db_path=":memory:")`, and `portal_client()`, which builds a `TestClient` and patches `get_store` to return that in-memory store across `app.py`, `helpers.py`, `routes/webhooks.py`, `routes/health.py`, `routes/schedules.py`.
- 20 test files import `AssessmentStore` directly or use `make_store()`/`get_store()`: `test_automode.py`, `test_automode_extended.py`, `test_browser.py`, `test_comprehensive.py`, `test_cve_autofix.py`, `test_dispatcher.py`, `test_durability.py`, `test_end_to_end.py`, `test_error_recovery.py`, `test_live_cluster_e2e.py`, `test_multi_app_fleet.py`, `test_onboarding_summary_parity.py`, `test_portal.py`, `test_remediation_loop.py`, `test_skill_inventory.py`, `test_slo_tracker.py`, `test_store_extended.py`, `test_watch.py`, `test_workflows.py`, plus `conftest.py` itself.
- Those 20 files contain **376 `def test_...` functions** (out of 937 total across the whole 80-file suite — see [Testing strategy](#testing-strategy) for what that number actually implies).
- `test_portal.py` alone accounts for 120 of those — but most exercise the store indirectly through the `portal_client` fixture's `TestClient` (HTTP-level), not direct `store.*` calls, which changes the shape of the required rewrite there (see below).

**Bottom line: ~12 application files + ~21 test files (33 total) reference `AssessmentStore` in some form.** That is the accurate scope for "the deferred rewrite," and it is why this is being sequenced as a separate, dedicated effort rather than squeezed in alongside tonight's parallel bug-fix batch.

## 2. HA Postgres deployment approach: CloudNativePG

**Chosen: [CloudNativePG](https://cloudnative-pg.github.io/) (CNPG), installed via OLM `Subscription`, chart ships only the `Cluster` CR.**

This follows the exact convention this chart already uses for Kafka: Strimzi is assumed pre-installed cluster-wide via OLM (see `docs/deployment.md`), and `chart/templates/kafka/kafka-cluster.yaml` only renders the `Kafka`/`KafkaNodePool` custom resources, gated behind `.Values.kafka.enabled`. `chart/templates/postgres/` (added tonight) mirrors this exactly: `.Values.postgres.enabled` (default `false`) gates a 3-instance CNPG `Cluster` CR plus an app-credentials `Secret`.

Why CNPG over the alternatives considered:

| Option | Verdict |
|---|---|
| **CloudNativePG** (chosen) | CNCF project, Kubernetes-native failover (uses the K8s API itself for leader election — no separate DCS process like Patroni/etcd to operate), Red Hat-certified on OpenShift OperatorHub (`cloud-native-postgresql` package), increasingly the default recommendation industry-wide as of 2026. Declarative CRDs for cluster, backup, and scaling match this repo's existing "everything is a CR the chart renders" style. |
| Crunchy PGO | Also Red Hat-certified on OpenShift OperatorHub, mature and widely deployed, but runs Patroni (etcd/DCS) + pgBouncer as extra moving parts inside the architecture, and Crunchy's certified-operator channel requires a registration token for upgrades — an extra operational dependency this project doesn't need. Reasonable choice if commercial support were a hard requirement; it isn't stated as one here. |
| Bitnami Postgres-HA Helm chart | No operator/OLM dependency, fully self-contained — but that's also the downside: no OLM-managed lifecycle, and *we* would own StatefulSet + Patroni + Pgpool failover logic directly in this chart, which is strictly more code and more to operate than a CR gated behind `enabled: false`. Breaks the "operator via OLM" convention this project already established for Kafka for no clear benefit given OpenShift's OLM is already a first-class part of this platform. |

Chart changes made tonight (already implemented, verified with `helm lint` and `helm template`, covered by `tests/test_helm_templates.py`):

- `chart/templates/postgres/postgres-cluster.yaml` — CNPG `Cluster`, `instances: 3` (1 primary + 2 replicas), pod anti-affinity across nodes, per-instance PVC sized by `.Values.postgres.storageSize`, optional `barmanObjectStore` backup block gated on `.Values.postgres.backup.enabled`.
- `chart/templates/postgres/postgres-secret.yaml` — app-user credentials `Secret` (`kubernetes.io/basic-auth`), using Helm's `lookup` to preserve an already-generated password across re-renders (avoids Argo CD seeing a spurious diff / rotating live credentials on every sync).
- `chart/values.yaml` — new `postgres.*` block: `enabled` (default `false`), `instances`, `storageSize`, `storageClassName`, `credentials.{secretName,username,database}`, `resources`, `backup.{enabled,destinationPath,credentialsSecretName,retentionPolicy}`.
- `docs/deployment.md` — new "Operator prerequisites" section documenting the CNPG `Subscription` the same way Strimzi is documented for Kafka.
- `tests/test_helm_templates.py` — `TestPostgresCluster` / `TestPostgresSecret` classes.

None of this is wired to the application yet — `postgres.enabled` defaults to `false`, and `store.py` still talks to SQLite.

## 3. Async Python library: `asyncpg`

**Chosen: [`asyncpg`](https://github.com/MagicStack/asyncpg)**, added to `pyproject.toml` tonight (dependency only, not imported anywhere yet).

`store.py` is deliberately raw-SQL, not an ORM, across all 16 tables — every method hand-writes its own `SELECT`/`INSERT`/`UPDATE` with `?`-style placeholders and manual `dict(row)`/`json.loads()` marshaling. Given that existing style:

- **`asyncpg`** — closest drop-in replacement for this style. Fast (binary protocol, no text parsing), no ORM/query-builder layer to learn or fight, connection pooling built in (`asyncpg.create_pool()`), and `$1`/`$2` positional placeholders are a mechanical find-replace from SQLite's `?`. **Chosen.**
- `psycopg` v3 async + `psycopg_pool` — DB-API-familiar (`?`→`%s`-ish semantics are closer to the existing sqlite3 calls in spirit), solid type handling, would also work fine. Passed over only because `asyncpg` has slightly less migration friction for this specific codebase's placeholder style and is the more common choice for greenfield async-only code paths (no need for psycopg's sync/async dual-mode support, since this app has no sync DB call sites once the migration completes).
- SQLAlchemy 2.0 async + `asyncpg`/`psycopg` driver — rejected. Adds an ORM/Core query-builder layer that raw-SQL-style code like `store.py` doesn't need; every one of its ~90 methods would need translating into Core `select()`/`insert()` constructs (or raw `text()` escapes, at which point SQLAlchemy is providing no value) for no functional gain. More churn than justified.

## 4. Schema translation notes (SQLite → Postgres)

### Type mapping

| SQLite type/pattern (as used in `store.py`) | Postgres equivalent | Notes |
|---|---|---|
| `TEXT` (all `id`/uuid columns, e.g. `id TEXT PRIMARY KEY`) | `TEXT` | No change — `uuid.uuid4().hex` strings work as-is. Could tighten to `UUID` type later; not required for a 1:1 port. |
| `TEXT NOT NULL` for ISO-8601 timestamp columns (`assessed_at`, `created_at`, `updated_at`, `timestamp`, `resolved_at`, `completed_at`, `last_heartbeat`, `registered_at`, `processed_at`) | `TIMESTAMPTZ` | Every timestamp in the codebase is written via `datetime.now(timezone.utc).isoformat()` — a `TIMESTAMPTZ` column accepts that string directly on insert and gives proper indexed date comparisons instead of ISO-string `<`/`>` comparisons (which happen to work in SQLite by lexicographic luck, but shouldn't be relied on in Postgres). |
| `TEXT` columns holding JSON blobs (`report_json`, `files_json`, `orchestration_json`, `applied_json`, `skipped_json`, `errors_json`, `repo_files_json`, `capabilities`, `details_json`, `snapshot_json`, `steps_completed`) | `JSONB` | `JSONB` gets indexing/querying for free and avoids the app doing `json.loads()`/`json.dumps()` on every read/write if any future query wants to filter inside the blob. At minimum, even keeping app-side (de)serialization, `JSONB` catches malformed JSON at write time that `TEXT` silently allows. |
| `REAL` (`overall_score`, `target_value`, `current_value`) | `DOUBLE PRECISION` | Direct equivalent. |
| `INTEGER` used as boolean (`dry_run`, `enabled`) | `BOOLEAN` | SQLite has no native boolean; the code does `int(dry_run)`/`bool(row["dry_run"])` round-trips. Postgres `BOOLEAN` removes that translation layer — the store's `save_apply_results`/`get_apply_results` and `toggle_schedule` methods can pass Python `bool` straight through. |
| `INTEGER PRIMARY KEY AUTOINCREMENT` (`apply_results.id`, `skill_inventory_snapshots.id`) | `GENERATED ALWAYS AS IDENTITY` (or `BIGINT GENERATED ALWAYS AS IDENTITY` if row counts could ever be large) | Postgres has no `AUTOINCREMENT` keyword; `IDENTITY` columns are the modern equivalent (`SERIAL` also works but is legacy-flavored). |
| Composite `PRIMARY KEY (skill_name, app_name, created_at)` (`skill_effectiveness`) | Same, unchanged | Postgres supports composite PKs identically. Worth noting `created_at` in a PK is fragile (two inserts in the same microsecond could theoretically collide) — consider adding a surrogate `id` in the rewrite, but that's a schema improvement, not a required translation. |
| `FOREIGN KEY (assessment_id) REFERENCES assessments(id)` (`onboarding_results`, `gates`, `remediations`, `slos`, `apply_results`) | Same, unchanged | Direct port. SQLite requires `PRAGMA foreign_keys = ON` to actually enforce these (see below); Postgres enforces FKs unconditionally, which will surface any FK-violating data that SQLite was silently allowing before the `PRAGMA` was set (or before it existed, for tables created before that line was added — check for orphaned rows before cutover if you *do* migrate the data, see §Data migration). |

### SQLite-specific syntax with a different Postgres equivalent

| SQLite (as found in `store.py`, with line refs at time of writing) | Postgres equivalent |
|---|---|
| `PRAGMA journal_mode=WAL` / `PRAGMA busy_timeout=5000` / `PRAGMA foreign_keys = ON` (lines 18-20) | No equivalent needed — these exist to work around SQLite's single-writer-file model (WAL for concurrent readers, busy_timeout for write-lock contention, foreign_keys because it's off by default). Postgres's MVCC engine and default FK enforcement make all three moot; just delete them in the rewrite. |
| `INSERT OR REPLACE INTO settings ...` (line 267), `INSERT OR REPLACE INTO agent_registry ...` (line 809), `INSERT OR REPLACE INTO suppressed_checks ...` (line 1278) | `INSERT INTO ... ON CONFLICT (key_columns) DO UPDATE SET ...`. Each call site needs its actual conflict target identified: `settings` conflicts on `key`, `agent_registry` conflicts on `agent_name` (has a `UNIQUE` constraint), `suppressed_checks` conflicts on its synthetic `id` (`f"{app_name}:{check_source}"`) or, better, on the real `UNIQUE(app_name, check_source)` constraint already declared on that table. |
| `INSERT OR IGNORE INTO processed_webhooks ...` (line 1040) | `INSERT INTO processed_webhooks ... ON CONFLICT (delivery_id) DO NOTHING`. |
| `ALTER TABLE ... ADD COLUMN ...` wrapped in `try/except sqlite3.OperationalError` for idempotent migrations (lines 48-59, 152-158) | Postgres supports `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` natively — the try/except dance becomes unnecessary. More broadly, the rewrite should move these ad-hoc inline migrations to a real migration tool (see [Suggested phased execution order](#suggested-phased-execution-order)) rather than porting the "run ALTER on every connect and swallow the error" pattern as-is. |
| `AUTOINCREMENT` | See type mapping above (`GENERATED ALWAYS AS IDENTITY`). |
| `strftime` / other SQLite date functions | **Not used anywhere in `store.py`** — confirmed by grep. All date logic is done in Python (`datetime.now(timezone.utc)`, `timedelta`, `.isoformat()`) before values ever reach SQL, and comparisons in SQL are plain string `<`/`>` against ISO-8601 text (e.g. `purge_old_data`'s `WHERE {col} < ?` with a computed cutoff string). This actually ports cleanly once those columns become `TIMESTAMPTZ` — just pass the same Python `datetime` objects (or their `.isoformat()` strings; `asyncpg` accepts both) instead of pre-formatted text. |
| `sqlite3.Row` row factory + `dict(row)` | `asyncpg` returns `asyncpg.Record` objects; `dict(record)` works the same way, so every `[dict(r) for r in rows]` list-comprehension idiom in `store.py` ports unchanged. |

### Full table list (16 tables)

`assessments`, `onboarding_results`, `events`, `gates`, `remediations`, `agent_registry`, `slos`, `apply_results`, `settings`, `remediation_jobs`, `scheduled_operations`, `processed_webhooks`, `agent_feedback`, `skill_effectiveness`, `suppressed_checks`, `skill_inventory_snapshots`.

(This list is directly confirmed by `AssessmentStore.export_all()`, which already enumerates all 16 for disaster-recovery export — that method doubles as a built-in schema inventory and is worth reusing as the seed list for whatever migration/dump tooling gets built.)

## 5. Connection/pooling strategy

Five long-running processes will each need their own `asyncpg` pool, sized for very different concurrency profiles:

| Component | Deployment | Concurrency profile | Suggested pool size | Notes |
|---|---|---|---|---|
| Portal | `agentit` (2 replicas) | FastAPI, naturally async-compatible already (`uvicorn` + `async def` routes exist in `app.py`) — handles concurrent HTTP requests | `min_size=5, max_size=20` per pod (so 10-40 connections across 2 replicas) | The portal is the only component that's already async end-to-end at the framework level; wiring `asyncpg.create_pool()` into its FastAPI lifespan (`@app.on_event("startup")`/lifespan context manager) is the smallest lift of the five. |
| `vuln-watcher` | separate Deployment, 1 replica | Single background loop, one fleet scan per tick | `min_size=1, max_size=3` | Needs `def run(self)` → `async def run(self)`, `time.sleep(interval)` → `await asyncio.sleep(interval)`, and the whole thing launched via `asyncio.run(watcher.run())` at the CLI entry point. |
| `slo-tracker` | separate Deployment, 1 replica | Same shape as vuln-watcher, tighter interval (5m default) | `min_size=1, max_size=3` | Same async conversion as vuln-watcher. |
| `drift-detector` | separate Deployment, 1 replica | Same shape, also talks to the cluster API (`PlatformContext`) each tick | `min_size=1, max_size=3` | Same async conversion; the Kubernetes client calls it makes are independent of the DB pool sizing. |
| `skill-learner` | separate Deployment, 1 replica | Least frequent tick (24h default), does LLM calls | `min_size=1, max_size=2` | Same async conversion. **Also noted as a pre-existing gap independent of Postgres**: `chart/templates/agents/skill-learner.yaml` currently does not mount the shared data PVC or set `AGENTIT_DB_PATH`, so today it actually runs against its own ephemeral, isolated SQLite file rather than the shared one the other 4 components use. Worth flagging explicitly during cutover planning — the Postgres migration will implicitly "fix" this (every component will point at the same connection string/secret) but that behavior change should be called out, not just silently inherited. |

General guidance: each watcher's `def run(self)` synchronous tick loop becomes `async def run(self)`, with `time.sleep(self._interval)` replaced by `await asyncio.sleep(self._interval)`, and the process entry point (currently a plain function call from `cli.py`'s Click command) wrapped in `asyncio.run(...)`. The pool itself should be created once at process startup (not per-tick) and passed into the watcher's constructor alongside (or instead of) today's `store: AssessmentStore` parameter.

## 6. Data migration approach

**Recommendation: clean cutover, no automated data migration script.**

Rationale: this is pre-production/demo data (fleet assessments, onboarding results, event history) with no external customers depending on historical continuity. A one-time dump-and-load script is possible (SQLite → CSV/JSON export per table via the already-existing `export_all()` method → `COPY`/bulk `INSERT` into Postgres) but the engineering cost of writing and testing a correct, FK-order-aware loader for 16 tables is not justified by data that can simply be regenerated by re-running assessments against the fleet.

If a specific need to preserve some data emerges later (e.g. a demo that must show historical trend charts), `export_all()` already produces the exact JSON shape needed as a starting point for a small ad-hoc script — but treat that as a "build it if and when needed" task, not a Phase 1 blocker.

## 7. Backward-compat / rollout strategy

**This cannot be a gradual/canary rollout at the data layer.** The portal (2 replicas) and all 4 watchers read and write the same logical state. If some pods are on SQLite (reading/writing `/data/agentit.db` on the shared RWO PVC) while others are on Postgres, they will silently diverge — writes to one backend are invisible to the other, and there is no dual-write or replication bridge planned or justified for demo-scale data.

**Required approach: a single coordinated cutover, all 5 Deployments updated in the same Argo CD sync.**

Practical implications for whoever executes this phase:

- The `postgres.enabled` chart flag existing today is *not* a safe "flip it on gradually" mechanism by itself — it controls whether the `Cluster` CR exists, not which backend the app code talks to. The actual backend switch happens in `store.py` and needs a single `AGENTIT_DB_BACKEND=postgres` (or equivalent) cutover across every Deployment simultaneously, not a per-Deployment rollout.
- Argo CD's canary `Rollout` strategy on the portal Deployment (`chart/templates/deployment.yaml`, `rollout.enabled: true`) is currently used for *code* rollouts (new image, gradual traffic shift) — it is not a safe mechanism for a *storage backend* change, since old and new portal pods would both be live simultaneously talking to different databases. **Recommendation: disable/bypass the canary steps specifically for the PR that flips the storage backend** (e.g. a temporary `rollout.steps` override to go straight to 100%, or coordinate a maintenance window), then re-enable normal canary behavior for the next ordinary code change.
- Because the watchers aren't behind Argo Rollouts (plain `Deployment`s, 1 replica each), a normal `kubectl`/Argo CD rolling update on those recreates the single pod — brief downtime per watcher during cutover is expected and acceptable (these are background jobs, not user-facing).
- **Flag this explicitly as the single biggest deployment risk of this whole migration**: an accidental partial rollout (e.g. Argo CD syncing the portal but a watcher's sync failing/lagging) would produce silent data divergence with no error surfaced anywhere. Whoever executes Phase 3 (below) should plan a smoke test immediately after cutover that confirms all 5 components are pointed at Postgres (e.g. a shared `/readyz`-style check that reports which backend it's connected to) before considering the sync "done."

## 8. Testing strategy

Current state: `tests/conftest.py`'s `make_store()` creates a synchronous in-memory SQLite store (`AssessmentStore(db_path=":memory:")`), and everything downstream — 376 test functions across 20 files plus `conftest.py`'s two central fixtures — calls store methods synchronously.

Once `store.py` is `async def` throughout:

- **A real Postgres instance is required for tests** — there is no async in-memory Postgres equivalent (unlike SQLite's `:memory:`). Two realistic options:
  - **`testcontainers-python`** (`testcontainers[postgres]`) — spins up a real Postgres container per test session via Docker/Podman. Best fidelity, but requires a container runtime in CI and adds real per-test latency (container startup).
  - **A Postgres service container in CI** (e.g. a GitHub Actions `services:` block or equivalent), with tests connecting to a fixed `localhost` port. Faster (one shared instance for the whole run) but requires CI config changes and loses the "fully isolated per-test DB" property unless combined with per-test schema/transaction rollback.
  - Recommendation: start with a **session-scoped `testcontainers` Postgres fixture + per-test transaction rollback** (wrap each test in a transaction that's rolled back at teardown, rather than recreating the whole schema per test) — this keeps test isolation close to what `:memory:` SQLite gave for free, without needing a new CI service definition on day one. Migrate to a CI-native Postgres service later if `testcontainers` startup overhead becomes a real bottleneck.
- **Mechanical scope of the test rewrite**: `make_store()` becomes `async def make_make_store()` (or an async fixture), every one of the 376 test functions in the 20 files listed in §1 that call store methods directly needs to become `async def test_...` with `await` added to each store call. This is large but *mechanical* — it is a systematic find/replace-shaped change, not a logic rewrite, and should be scripted (e.g. a codemod pass) rather than done by hand file-by-file.
- **`test_portal.py` (120 tests) is a special case**: most of its tests go through the `portal_client` fixture's `TestClient`, i.e. they call HTTP endpoints, not `store.*` directly. FastAPI's `TestClient` already handles async route handlers transparently, so most of those 120 tests likely **do not** need to become `async def` themselves — only the *fixture setup* in `conftest.py` (`store = make_store(); store.save(report); ...`) needs to become async (and the fixture itself needs `pytest-asyncio` or equivalent to bridge into the sync `TestClient` call). This meaningfully reduces the "must become async" count below the full 376 — worth re-auditing file-by-file during Phase 3 rather than assuming every one of the 376 needs a signature change.
- Add `pytest-asyncio` (or use `anyio`, which `httpx`/`starlette` already depend on) to `pyproject.toml`'s dev dependencies when this phase starts.

## 9. Suggested phased execution order

1. **Phase 1 — Stand up Postgres HA + migrate schema.** Set `postgres.enabled: true`, verify the CNPG `Cluster` reaches `Cluster Ready` status on the target OpenShift cluster. Write the Postgres DDL (using the type mapping in §4) as a proper migration (e.g. `alembic` for offline SQL migrations, or a hand-rolled idempotent `CREATE TABLE IF NOT EXISTS` script mirroring today's inline pattern, run once at Postgres cluster creation). No application code changes yet.
2. **Phase 2 — Rewrite `store.py` to async/`asyncpg`, same public method signatures.** Every method becomes `async def`, gains `await` on the pool call, `?` placeholders become `$1`/`$2`/…, `INSERT OR REPLACE`/`INSERT OR IGNORE` become `ON CONFLICT` (per §4's table). Keep method names and parameter/return shapes identical wherever possible so Phase 3's call-site edits are `def` → `async def` + `await` mechanical changes, not logic rewrites. `AssessmentStore.__init__` becomes an async factory (e.g. `await AssessmentStore.create(dsn)`) since pool creation is itself async.
3. **Phase 3 — Migrate callers file-by-file, tests passing at each step.** Suggested order, roughly matching the call-site inventory in §1 from smallest/simplest to largest/riskiest: `cli.py` → `remediation/dispatcher.py` → `remediation_loop.py` → each of the 4 watchers (`vuln_watcher.py`, `slo_tracker.py`, `drift_detector.py`, `skill_learner.py`, each including its `asyncio.run()`/pool-sizing wrapper from §5) → `portal/helpers.py` → `portal/routes/*.py` → `portal/app.py` last (largest surface area, most routes). Convert each file's corresponding tests in lockstep, per the mechanical `async def`/`await` pattern in §8 — do not let test conversion lag behind app-code conversion by more than one file, or the suite will be red for an extended period.
4. **Phase 4 — Remove SQLite entirely.** Delete the `sqlite3` import and any leftover SQLite-specific code paths from `store.py`, remove `AGENTIT_DB_PATH` env var wiring from all 5 Deployments in the chart, and re-evaluate the shared `/data` RWO PVC (`chart/templates/pvc.yaml`) — keep it only if something else still needs `/data` (currently nothing does, once the DB is the only consumer of that mount), otherwise remove the PVC and its backup CronJob (`chart/templates/pvc-backup.yaml`, `chart/templates/workflows/db-backup-cronjob.yaml`) since CNPG's own `barmanObjectStore` backup mechanism (already plumbed in tonight's `postgres.enabled` chart work, gated behind `postgres.backup.enabled`) supersedes the SQLite-file `sqlite3 .backup` CronJob.
