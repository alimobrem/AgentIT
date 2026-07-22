# Resilience audit — 2026-07-18

**Status: 4 fragility fixes implemented and shipped; the rest of this doc
is a prioritized, evidence-based backlog, not a promise.**

## Why this doc exists

2026-07-17/18 was an unusually concurrent day for this repo (~10+ workers
active at once), and a separate stream of work that day fixed several real
bugs one incident at a time: a webhook check-then-act dedup race
(`f629860`), a `create_gate`/`save_remediation` dedup race (`d33b61a`), a
webhook dedup time-bucket bug (`79f86cb`), four unlocked TTL-cache dicts
(`1026571`), and `kube_breaker` wiring into `kube.py`'s real API calls
(`0d15cd9`/`e55cc97`). Those were all found reactively, one observed
symptom at a time. This audit is the deliberately *proactive* counterpart:
instead of waiting for the next symptom, it systematically walked the
same six fragility categories (concurrency, external-dependency
resilience, single points of failure, resource exhaustion, cascading
failure, data-integrity-under-partial-failure) looking for the *next*
unfound instance of each pattern already proven to exist in this
codebase — on the theory that a bug class found once is worth checking
for everywhere, not just at its one known occurrence.

## Coordination note: relationship to the self-health-check watcher

A separate, concurrent stream of work (branch
`feature/agentit-self-health-watcher`, uncommitted as of this audit) is
building a periodic watcher (`watchers/self_health_check.py`) that
detects and surfaces four classes of AgentIT-*infrastructure* failure:
GitHub webhook reachability, CI pipeline stalls, maintenance CronJob
failures, and cleanup-CronJob effectiveness — see that branch's own
`docs/self-health-check-backlog.md` for its full design record. **Also
landed on `main` during this same audit** (commit `19c63fd`, a third,
independent stream of work): `github_pr.py::check_webhook_delivery_health()`
plus a new Health page "Webhook Deliveries" section — a live,
on-page-load check of the same "is GitHub actually delivering push
events to us" question, implemented differently (a synchronous Health
page check, not a periodic watcher event). **These two efforts now
overlap** (both answer "is AgentIT's own GitHub webhook actually
delivering") via two different mechanisms; reconciling that overlap is
this audit's one explicit non-recommendation — it belongs to whichever
of those two streams lands second, not to this one, since neither of
their branches is something this audit should edit out from under them.

This audit is explicitly **broader and more architectural** than either
of those: it looks at concurrency/race-condition patterns in shared
in-process state, resilience of AgentIT's *own* external dependencies
(LLM, Kubernetes, GitHub, Postgres) rather than AgentIT's dependents'
infrastructure, and data-integrity-under-partial-failure for AgentIT's
own multi-step workflows. Nothing below duplicates either of those two
efforts' actual checks; where a finding overlaps (GitHub webhook/API
resilience specifically), it's called out explicitly below.

## 1. Findings, by category

Each finding is tagged **[FIXED]** (this pass, with tests — see §2) or
**[BACKLOG]** (documented, not fixed — with a reason), plus a rough
likelihood/impact call.

### 1a. Concurrency / race conditions

| # | Finding | Likelihood | Impact | Disposition |
|---|---|---|---|---|
| 1 | `CircuitBreaker` (`portal/helpers.py`) — `record_failure()`/`record_success()`/`is_open` read-modify-wrote `_failures`/`_last_failure` with **zero locking**, despite `llm_breaker`/`kube_breaker` each being one shared instance called from many concurrent OS threads (every `LLMClient._chat()` call and most of `kube.py`'s real API-calling functions run inside `asyncio.to_thread`, plus every watcher's own thread). | High (many threads, no lock at all) | Medium — a lost failure-count update delays exactly the protection the breaker exists to provide, right when a dependency is genuinely struggling | **[FIXED]** |
| 2 | Three cache-bust call sites (`capabilities_learn_route`, `activate_skill_route`/reactivate, `deprecate_skill_route`, `webhooks.py`'s `webhook_skill_draft`) wrote `_skills_cache["data"] = None` **directly**, bypassing `_skills_cache_lock` — the exact lock the 2026-07-16 TTL-cache-locking fix (`1026571`) added for this same cache's *read* path. | Medium (needs a bust racing a concurrent refresh) | Low — a delayed-by-up-to-60s skill visibility, not data corruption | **[FIXED]** |
| 3 | `update_remediation_job()`'s `steps_completed` append — `SELECT` (no row lock), mutate in Python, `UPDATE` — is a lost-update race if two calls for the same `job_id` land concurrently with different `current_step` values. Same shape as the already-fixed `create_gate`/`save_remediation` races, just not caught in that pass. | Low-medium (today's callers are mostly sequential per job, but a webhook-retriggered onboard racing the original run's own progress update is a real, not-yet-observed scenario) | Medium — a silently-dropped progress step is confusing but not destructive | **[FIXED]** |
| 4 | `set_infra_repo_url()` does a full read-modify-write of the *entire* `report_json` blob (`get()` → mutate one field → `UPDATE ... SET report_json`) with no lock/version check. Two concurrent calls for the same `assessment_id` would have the last writer silently discard the other's change. | Low (single form submit, one caller site, `routes/assessments.py`) | Low | **[BACKLOG]** — real but the lowest-likelihood item found; worth a `FOR UPDATE`-style fix if this ever gets a second caller |
| 5 | `cancel_gate()` (`routes/gates.py`) calls `resolve_gate()` (the atomic claim) but never checks the returned bool, unlike `resolve_gate`'s own POST handler which does. Two concurrent "Dismiss" clicks on the same gate wouldn't double-apply any side effect (there is none for cancel), but the pattern is inconsistent with the rest of the file's own documented convention. | Low | Very low (no side effect to duplicate) | **[BACKLOG]** — cosmetic-severity, noted so a future gate type that *does* add a cancel-time side effect doesn't inherit this gap silently |

**Audited and found already correct** (no fix needed, listed so this
doesn't get re-audited from scratch next time): `resolve_gate()`/
`reopen_gate()` (atomic `UPDATE ... WHERE status = 'pending'` claim,
already fixed in an earlier pass), `create_gate()`/`save_remediation()`
(advisory-lock-serialized, already fixed), `claim_webhook()` (atomic
`INSERT ... ON CONFLICT`, already fixed), `register_agent()`/
`agent_heartbeat()` (`ON CONFLICT DO UPDATE` upserts, race-free by
construction), `_upsert_app()` (same), `get_store()`/
`get_nav_gate_badge_counts()` (double-checked locking with the correct
lock type already in place).

### 1b. External dependency resilience

| # | Finding | Likelihood | Impact | Disposition |
|---|---|---|---|---|
| 6 | `AssessmentStore.create()`'s `asyncpg.create_pool()` set no `command_timeout` (unbounded by default) and relied on asyncpg's own 60s default connect timeout. A wedged (not fully down — a lock wait, a runaway query on someone else's connection, a half-open TCP session) Postgres could hang any query **indefinitely**, and since every route holds its connection for that whole wait, enough concurrently-stuck requests exhaust the pool (`max_size=20`) and every *other* route needing the store hangs too. | Medium (a "wedged, not down" DB is a real failure mode, not just "fully unreachable") | High — total portal unavailability with zero user-facing signal, cascading from one slow query | **[FIXED]** |
| 7 | `github_pr.py` has dozens of individual `requests.get/post/patch` call sites, each with its own `timeout=` (10-30s, consistently applied — good), but **no circuit breaker** at all, unlike `llm_breaker`/`kube_breaker`. No rate-limit-aware backoff either: a GitHub 429/403 with a `Retry-After`/`X-RateLimit-Reset` header is treated as a generic failure, not a "come back at time X" signal. | Medium (GitHub rate limits are a real, documented failure mode for a fleet-wide tool making many API calls) | Medium — no cascading-failure protection for a GitHub outage/rate-limit storm, though each individual call still fails fast (bounded timeout) rather than hanging | **[BACKLOG]** — deliberately not fixed this pass: `github_pr.py` had 3 independent commits land on `main` *during this same audit* (`538a282`, and the two AutoMode-removal/webhook-health commits, `ebeed09`/`19c63fd`), making it the single hottest file in the repo today. Retrofitting a breaker across dozens of call sites in a file under this much concurrent, unrelated churn is a large-surface-area change for a real-but-bounded (not unbounded-hang) risk — the wrong trade to make today. **Recommended next step**: once `github_pr.py` settles, add a `github_breaker` (same shape as `llm_breaker`/`kube_breaker`) around the small number of *mutating* calls first (`create_pr`, `merge_pr`, `ensure_webhook`) rather than all read paths, and add `Retry-After`-aware backoff specifically to `RemediationLoop`'s retry path (the one caller that already retries). |
| 8 | Kafka: already resilient — `EventPublisher.publish()` falls back to a local SQLite buffer (`_buffer_locally()`) on any failure, with a bounded `flush(timeout=5)`. No fix needed. | — | — | **Audited, already correct** |
| 9 | LLM/Kubernetes: already resilient — both have circuit breakers, per-call timeouts, and fail-closed/safe-fallback contracts per caller (see README's existing "Circuit breakers" note). The only gap found in this area was #1 above (the breaker's own thread-safety), now fixed. | — | — | **Audited, already correct** (modulo #1) |

### 1c. Single points of failure

| # | Finding | Likelihood | Impact | Disposition |
|---|---|---|---|---|
| 10 | Postgres is a single instance with no documented HA/failover story — every watcher, the portal, and every CLI command depend on it, and there is no fallback store. | High (single instance, by design, for this app's scale) | High if it happens, but this is a known, accepted trade-off for a small internal tool, not an oversight | **[BACKLOG, accepted risk]** — out of scope to "fix" (would mean standing up Postgres HA/replication, a deployment-topology change, not an application-code fix). What *is* in scope and now fixed: #6 above, so that when Postgres degrades (rather than being fully down), the failure mode is a fast, clear, recoverable timeout instead of a silent, unbounded hang. |
| 11 | The watcher fleet (6+ watchers) is **already well-isolated**: each watcher is its own Kubernetes Deployment/pod (`chart/templates/agents/*.yaml`), each with its own liveness probe keyed off a per-process `/tmp/heartbeat` file, and every watcher's main loop (`watchers/__init__.py::record_tick()`/`sleep_with_heartbeat()`) catches and logs per-tick exceptions rather than propagating them. A crash/hang in one watcher cannot take down another, or the portal. | — | — | **Audited, already correct** — this is the one SPOF-adjacent question this audit expected to find a real gap in, and didn't; noted so it isn't re-investigated from scratch next time. |

### 1d. Resource exhaustion

| # | Finding | Likelihood | Impact | Disposition |
|---|---|---|---|---|
| 12 | Postgres connection-pool exhaustion via a wedged query — same finding as #6 above, listed again here since it's simultaneously an external-dependency-resilience gap *and* a resource-exhaustion one (the mechanism of harm is pool exhaustion). | — | — | **[FIXED]** (see #6) |
| 13 | `asyncio.to_thread`'s default executor is sized `min(32, os.cpu_count() + 4)` — every blocking call this app offloads (git clone+assess, LLM calls reached through it, `kube.py` calls) shares this one pool per process. A burst of concurrent long-running assessments could, in principle, serialize behind this shared pool. | Low — audited the actual call sites; every assess/onboard/LLM/kube call already goes through a bounded timeout (`with_timeout`, per-call `timeout=`), so a saturated executor produces *queueing delay*, not an unbounded hang, and this app's real traffic profile (an internal ops tool, not a public-facing high-QPS service) makes a executor-saturating burst unlikely in practice | **[BACKLOG]** — noted for awareness; not fixed since no evidence of it actually happening, and a dedicated executor per call class would be a real architecture change for a theoretical, not observed, problem. |
| 14 | Temp-directory/file-descriptor hygiene for cloned repos (`clone_assess_cleanup`) — audited: already wrapped in `try/finally: shutil.rmtree(repo_path, ignore_errors=True)`, so a clone/assess failure never leaks disk space. | — | — | **Audited, already correct** |

### 1e. Cascading failure modes

| # | Finding | Likelihood | Impact | Disposition |
|---|---|---|---|---|
| 15 | The one *real* cascading-failure mechanism this audit found, beyond what's already covered above: a wedged Postgres query cascading into "every other route needing the store hangs too" (#6). No evidence of any *other* cross-subsystem cascade (e.g. one watcher's failure corrupting another's state, or a CI-pipeline stall blocking an unrelated deploy step the way today's earlier incident did) — the watcher-isolation finding (#11) is exactly why: separate pods, separate DB connection pools (even though they share one Postgres instance), separate liveness probes, per-tick exception isolation. | — | — | Covered by #6's fix; no additional cascading-failure-specific fix needed beyond that. |

### 1f. Data integrity under partial failure

| # | Finding | Likelihood | Impact | Disposition |
|---|---|---|---|---|
| 16 | `reap_orphaned_jobs()` already handles the `remediation_jobs` table (assess/onboard jobs orphaned by a dead process) — audited as correct and complete for that table. | — | — | **Audited, already correct** |
| 17 | The `deliveries` table has **no equivalent reaper**: `route_and_deliver()` creates a delivery row (`status="in_progress"`) and, on any *raised* exception, correctly marks it `"failed"` (already fixed in an earlier pass, per that code's own comment) — but a hard process kill (pod eviction, OOM, a rolling redeploy) between the `create_delivery()` call and that `try/except` completing leaves the row stuck at `"in_progress"` forever, identical in shape to the `remediation_jobs` orphan case `reap_orphaned_jobs()` already exists to fix. | Low-medium (delivery is a synchronous, in-request operation involving network calls — real but shorter-duration exposure window than the async `remediation_jobs` background jobs `reap_orphaned_jobs` already covers) | Medium — a permanently-stuck "in_progress" delivery is confusing on Ledger/PR History, though not itself destructive (no lost data, just a stale status) | **[BACKLOG]** — same fix shape as `reap_orphaned_jobs()` (a periodic `UPDATE deliveries SET status = 'failed' WHERE status = 'in_progress' AND created_at < now() - interval`), not implemented this pass to keep this audit's direct changes scoped to the 4 highest-value items; straightforward to add as a follow-up once picked up. |
| 18 | Gate creation (`create_gate()`) mid-flight interruption: audited — a gate is created in one atomic transaction (advisory-lock-serialized insert), so there's no partial-gate state possible; a crash before that transaction commits simply means the gate was never created (recoverable — the triggering event can be re-processed), and a crash after commit leaves a normal, resolvable `pending` gate. No fix needed. | — | — | **Audited, already correct** |

## 2. What's fixed (this pass)

Four items, chosen as the highest-value/most-tractable subset (per this
task's own scope guidance) rather than an attempt to fix everything
found:

1. **`CircuitBreaker` thread-safety** (`portal/helpers.py`) — added a
   `threading.Lock` around `record_failure()`/`record_success()`/
   `is_open`'s critical section. Both `llm_breaker` and `kube_breaker`
   inherit the fix automatically (one shared class).
2. **Postgres pool timeouts** (`portal/store.py`) — `AssessmentStore.
   create()` now passes `command_timeout` (default 30s) and
   `connect_timeout` (default 15s, tighter than asyncpg's own 60s
   default) to `asyncpg.create_pool()`, both exposed as override-able
   parameters for testability.
3. **`update_remediation_job()` lost-update race** (`portal/store.py`) —
   the `steps_completed` read-modify-write now locks the row with
   `SELECT ... FOR UPDATE` inside its existing transaction.
4. **Skills-cache-bust lock bypass** (`portal/routes/capabilities.py`,
   `portal/routes/webhooks.py`) — added `capabilities.bust_skills_cache()`,
   a small locked helper, and routed all four call sites through it
   instead of a bare dict write.

Each fix has a dedicated regression test that was **verified to fail
against the pre-fix code** (not just "written to pass") — see §3.

## 3. Fault-injection testing — the pattern, and what's new

### What already existed (extended, not duplicated)

`tests/test_kube_breaker.py` already established a solid fault-injection
pattern for `kube_breaker`: mock `kube.py`'s low-level client accessors
to raise real-shaped exceptions (`ApiException` with real status codes),
then assert (a) repeated failures open the breaker at the documented
threshold, (b) an open breaker skips the real API call and returns each
function's own documented safe-fallback contract, (c) success resets the
failure count, and (d) specific non-failure conditions (404, 409,
`AGENTIT_OFFLINE`) never count against the breaker. `tests/
test_ttl_cache_locking.py` established a second pattern for proving lock
usage deterministically: swap the real lock for an instrumented one and
count concurrent holders (or, for a too-fast-to-race critical section,
time how long a call blocks against a lock deliberately held open on
another thread) rather than trying to reproduce a race probabilistically.

### What's new in this pass

- **`tests/test_llm_breaker.py`** — extends `test_kube_breaker.py`'s
  exact fault-injection shape to `llm.py`'s `LLMClient._chat()`, which
  had graceful-failure tests (`test_llm_graceful.py`) but nothing proving
  repeated real failures actually trip `llm_breaker`, or that an open
  breaker actually skips the real Anthropic call. Also adds the
  deterministic lock-usage proof for `CircuitBreaker` itself (a tracking
  lock swapped in, driven by 20 real concurrent threads).
- **`tests/test_store_resilience.py`** — genuine fault injection against
  a *real* Postgres (not mocked): uses `pg_sleep()` to make a real
  connection wedged, then proves (a) the bounded `command_timeout`
  actually fires quickly rather than hanging, (b) the pool self-heals
  afterward (a timed-out connection doesn't poison the pool for
  subsequent queries), and (c) one wedged query doesn't block unrelated
  concurrent queries from completing.
- **`tests/test_store.py::TestUpdateRemediationJobConcurrentSteps`** —
  concurrent-calls test (`asyncio.gather`, 10 distinct steps for one
  `job_id`) proving the `FOR UPDATE` fix; placed next to the existing
  `create_gate` concurrency test for discoverability.
- **`tests/test_ttl_cache_locking.py`** — new timing-based test proving
  `bust_skills_cache()` actually blocks on `_skills_cache_lock` (a
  concurrency-counting test can't catch this specific bug, since the
  bust's own critical section is too fast to reliably overlap another
  holder in a naive holder-count).

**Every new concurrency/timing test in this pass was verified to fail
against the pre-fix code** before being accepted (not just written to
pass against the fix) — this is the same rigor `1026571`/`f629860`/
`d33b61a` already established, applied here too:

| Test | Verified failure mode against pre-fix code |
|---|---|
| `test_llm_breaker.py::TestConcurrentAccessIsThreadSafe` (both tests) | `max_concurrent == 0` / `AttributeError: no attribute '_lock'` |
| `test_store.py::test_concurrent_distinct_steps_are_all_recorded` | Reliably lost 2-8 of 10 concurrently-appended steps across 3 separate runs (never flaky-passed) |
| `test_ttl_cache_locking.py::test_bust_skills_cache_actually_blocks_on_the_read_paths_lock` | Returned in microseconds instead of blocking for the lock's hold duration |

### The general pattern, stated for reuse

For **circuit breakers / external dependencies** (mirrors
`test_kube_breaker.py`/`test_llm_breaker.py`): mock the client at its
lowest-level accessor (not the whole module), inject a real-shaped
exception, and assert against the three invariants every breaker-wrapped
function should have: (1) N failures open the breaker at its documented
threshold, (2) an open breaker skips the real call entirely
(`assert_not_called()`) and returns/raises exactly what that function's
*own* documented contract says for "dependency unavailable" (never a
generic exception), (3) a success resets the count. Never mock so deep
that the breaker-check/record-around-the-call wiring itself is bypassed
— the whole point is proving that wiring, not just the breaker class in
isolation.

For **locking correctness**: prefer a deterministic proof over a
probabilistic race reproduction. If the critical section has real work
inside it (a slow load, a network call), swap in a tracking lock and
count concurrent holders across many real threads — `max_concurrent`
must stay at 1. If the critical section is too fast for that (a single
dict write), hold the real lock open on a background thread and time how
long the call under test blocks — it must block for roughly the hold
duration, not return immediately.

For **Postgres/network-dependency timeouts**: prefer a *real* dependency
with an injected fault over a mock, when the fault is expressible at
that level (`pg_sleep()` for "wedged", closing a socket for
"disconnected", a wrong port for "unreachable") — this proves the actual
`asyncpg`/`requests`/`kubernetes` client configuration, not a
reimplementation of it in a mock.

## 4. Periodic resilience verification — proposal, not built here

The task prompt for this audit explicitly asked whether a periodic (not
just at-commit-time) synthetic check makes sense, and to coordinate with
the in-flight self-health-check watcher rather than duplicate it. Given
that watcher's scope is infrastructure-level ("is CI/webhooks/CronJobs
actually working"), the natural extension for *this* audit's own
findings — once the self-health-check watcher lands — is a fifth check
in that same watcher, not a new one:

- **`self-check-circuit-breakers`**: read `get_circuit_breaker_states()`
  and alert if any breaker has been open for longer than some threshold
  (e.g. 10 minutes) — a breaker that's been open a long time means its
  dependency has been failing for a long time with nobody having looked,
  exactly the same "nothing periodically re-checks AgentIT's own
  critical infrastructure" gap that watcher's own docstring describes,
  just for the resilience *mechanisms* themselves rather than the
  underlying dependency.
- **`self-check-db-pool-saturation`**: `asyncpg.Pool` exposes
  `get_size()`/`get_idle_size()` — a periodic check that the pool isn't
  sitting near `max_size` with near-zero idle connections would be an
  early warning for the exact pool-exhaustion scenario #6's fix now
  bounds the *worst case* of, but doesn't prevent the underlying cause
  from recurring.

Neither is built in this pass — they belong in `watchers/
self_health_check.py` once that branch lands, as two more entries in its
own `CHECK_ACTIONS` tuple, reusing its existing dual-write-to-Kafka-and-
store convention and Health-page panel. Building a second, competing
watcher here would be exactly the duplication this task asked to avoid.

## 5. How do we know we're getting more resilient over time?

A concrete, checkable answer, not a vague claim:

1. **A growing, named test suite specifically for resilience, not just
   correctness.** `test_kube_breaker.py` (pre-existing) → `test_llm_breaker.py`
   (this pass) → `test_store_resilience.py` (this pass) establish a
   consistent, reusable fault-injection pattern per external dependency
   (see §3's "general pattern" — the point of writing it down explicitly
   is so the *next* dependency added to this app has an obvious template
   to follow, not a from-scratch design exercise). `test_ttl_cache_locking.py`
   does the same for locking correctness. Run them explicitly:

   ```bash
   uv run pytest tests/test_kube_breaker.py tests/test_llm_breaker.py \
     tests/test_store_resilience.py tests/test_ttl_cache_locking.py \
     tests/test_durability.py -q
   ```

   These already run as part of the full suite in CI (`tests/` has no
   opt-in marker excluding them) — no separate CI wiring was needed.
2. **This doc itself, re-run periodically as a checklist, not a one-time
   report.** The six categories in §1 (concurrency, external-dependency
   resilience, SPOFs, resource exhaustion, cascading failure,
   data-integrity-under-partial-failure) are a repeatable audit
   structure — the next resilience pass should re-check each category
   against whatever's new in the codebase by then (new external
   dependencies, new multi-step workflows, new shared in-process state),
   the same way this pass explicitly re-checked every pattern the
   2026-07-17/18 reactive fixes had already proven exists in this
   codebase at least once.
3. **The backlog in §1 is the honest, prioritized "what's next"** — every
   `[BACKLOG]` row states its own likelihood/impact and, where
   applicable, exactly why it wasn't fixed now (not "someday", a specific
   blocking reason: file churn, low observed likelihood, or an
   architecture-level trade-off). A future pass picking up item #7
   (GitHub breaker) or #17 (deliveries-table reaper) has a concrete,
   already-scoped starting point instead of re-discovering the gap.
4. **The Health page's Circuit Breakers table already gives a live,
   ambient signal** (`get_circuit_breaker_states()`, unaffected by this
   pass except for now being backed by a thread-safe implementation) —
   a breaker that's open right now is real-time, not just a historical
   test-suite fact.

This is deliberately not "we added N tests, therefore we are resilient"
— the concrete claim is narrower and checkable: these specific fixes
were proven (fail-before/pass-after) against these specific fault
scenarios, that proof lives in a test suite that runs on every commit,
and the categorized backlog names exactly what's still open and why.
