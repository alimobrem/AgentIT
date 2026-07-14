# AgentIT Project Rules

## Deployment

- **Never run `helm upgrade` manually** — Argo CD is the sole deployer. Manual helm commands conflict with Argo CD's server-side apply and the Rollouts controller, causing ownership fights on Services and pod specs.
- **To change config:** Edit `argocd/application.yaml` params, commit, push. Argo CD auto-syncs.
- **To update the running image:** The CI pipeline builds with the commit SHA as the image tag, then patches the ArgoCD Application's `image.tag` param. ArgoCD auto-syncs the new tag, triggering the Argo Rollout. For manual updates: push a new tag to the registry, then `oc -n openshift-gitops patch application agentit --type=json -p '[{"op":"replace","path":"/spec/source/helm/parameters/1/value","value":"<tag>"}]'`.
- **Never put secrets in values.yaml or any committed file** — the repo is public. Use `oc create secret` on-cluster and reference via Helm params.

## Code

- Never `# type: ignore` — fix the actual type issue.
- Never `except Exception: pass` — always `logger.exception("context")` or `logger.warning(...)`.
- LLM calls must always fail gracefully — catch all exceptions in `_chat()`, return `None`.
- LLM client init must always fail gracefully — if credentials are missing, continue without LLM.
- All agents follow the pattern in `agents/base.py` — take `(report, output_dir)`, return a result with `files: list[GeneratedFile]`.
- `GeneratedFile` and `_sanitize_name` live in `agents/base.py` — import from there, never redefine locally.
- `validate_manifest()` and `validate_generated_files()` live in `agents/base.py` — use to validate generated YAML.
- Shared analyzer utilities (IGNORED_DIRS, iter_text_files, calculate_score) live in `analyzers/base.py` — don't duplicate.
- Agent/check run history is tracked in structured SQLite tables (`agent_runs`, `check_results`), written by `FleetOrchestrator` and `runner.run_assessment`'s `check_results_out` param — don't reintroduce string-matching heuristics over `events.action` (that's exactly the bug `get_agent_stats()` used to have).
- `AssessmentStore.log_event()` takes an optional `correlation_id` — pass the `assessment_id` whenever the caller has one (see `save()`, `save_onboarding()`, `FleetOrchestrator._log_event`) so an assess → onboard → apply chain stays traceable end to end on the Events page.
- `agent_heartbeat()` upserts into `agent_registry` — never assume the row already exists for a long-lived watcher (they don't go through `register_agent`).
- Circuit breaker state is read via `portal/helpers.py::get_circuit_breaker_states()` — use that accessor instead of reaching into `CircuitBreaker` internals directly, so `/health` and the `agentit_circuit_breaker_open` gauge never drift from each other.
- **`portal/helpers.py::get_store()` is `async def`** — every caller (CLI, watchers, portal routes) gets the store via `await get_store()` (portal) or `await store_factory.create_store(...)` (CLI/watchers), never `AssessmentStore()` directly except inside `store.py`/`store_factory.py` themselves. Backend is selected by `AGENTIT_DB_BACKEND` (`sqlite` default, `postgres` opt-in) — see `docs/postgres-migration-plan.md`. Every store method call is `await`ed.
- **`FleetOrchestrator`, `AutoMode`, `RemediationDispatcher`, and `RemediationLoop` are genuinely `async def` throughout** (converted from the previous permanently-synchronous design — see `docs/postgres-migration-plan.md`'s "Progress update: sync→async conversion complete" section). Construct them with an async-compatible store (`await get_store()`'s return value directly — `AsyncSQLiteStore` or `store_pg.AssessmentStore`, never `.raw`) and `await` every call into them; every internal `self._store.method(...)` call inside these four classes is itself `await`ed. **No more `.raw`/`asyncio.to_thread` bridge for these four specifically** — that idiom is only for genuinely-synchronous consumers now (background assessment threads, `EventConsumer`, each watcher's own tick body, metrics/health/fleet-enrichment helpers).
- **Narrow `to_thread` at the specific blocking-I/O call site, not the whole class/method.** The four classes above still call into truly-synchronous things — the `kubernetes` Python client (`kube.py`), the synchronous Anthropic SDK client (`llm.py`'s `LLMClient.classify_action`), the 3 surviving Python agents' `.run()` methods (`agents/base.py`'s contract stays sync on purpose — not worth an async ripple through every agent for 3 classes). Wrap *only* that one blocking call in `asyncio.to_thread(...)` right where it happens (e.g. `orchestrator.py`'s `_run_agents_as_jobs` wraps each individual `kube.*` call, not the whole method) — never wrap an entire `async def` method (or the whole class) in `to_thread` just because it calls one blocking thing internally. This is the pattern to follow for any future sync-dependency inside an otherwise-async class.
- **`store_pg.AssessmentStore` has no `.raw`, on purpose.** Handing a synchronous-only consumer (background threads, `EventConsumer`, watcher tick bodies) a Postgres-backed store is exactly the kind of silent partial-cutover this codebase's Postgres migration plan (`docs/postgres-migration-plan.md` §7) warns against — that combination must fail loudly (e.g. `AttributeError`/an explicit check-and-raise), never silently degrade.
- **Never construct a second, separate `AsyncSQLiteStore(":memory:")` in tests that need to share data with an existing sync `AssessmentStore(":memory:")`** — each `:memory:` connection is its own isolated database. Use `AsyncSQLiteStore.wrap(existing_sync_store)` (in `store_factory.py`, or `conftest.py`'s `make_async_store()` helper) to get an async-compatible facade over the *same* connection instead.

## Self-monitoring CronJobs

- Any new CronJob that needs both `oc` (or another `openshift/cli`-image-only tool) and `curl`/`openssl` should split into an `initContainer` (cli image, writes its result to a shared `emptyDir`) + a main container (`ubi9/ubi-minimal`, which has curl) that reads that file and reports it — see `synthetic-probe-cronjob.yaml` and `secret-rotation-cronjob.yaml`. Don't assume the `cli` image ships curl; `chart/templates/tekton/pipeline.yaml`'s `report-status` task already switched images specifically because it doesn't.
- Every self-monitoring webhook (`/api/webhook/{synthetic-probe,backup-status,secret-check}`) follows the same shape as every other in-cluster-only route: `verify_internal_token`-gated, sets a Prometheus gauge, and logs an event on the bad-outcome path. Add new self-checks the same way rather than inventing a new reporting mechanism per check.
- A Secret that's auto-generated by this chart (e.g. `agentit-internal-webhook-token`) is safe to rotate automatically. A Secret whose value must match something external the chart has no credential to read/write (e.g. `github-webhook-secret`, which must match GitHub's own webhook config) must never be auto-regenerated on "missing" — that silently desyncs it from the external system the same way the 2026-07-13 incident happened. Only report its existence/drift; let a human recreate it with the correct value.

## Testing

- On a machine with a real (even if currently unreachable) `~/.kube/config`, `pytest` can hang for minutes: fleet/health routes call `kube.list_custom_resources(...)` unconditionally (by design — it's a `try`/`except`-wrapped resilience feature, not something to gate behind `--live-cluster`), and the Kubernetes client's `_request_timeout` doesn't bound DNS/TCP-connect time. Run tests with `KUBECONFIG=/tmp/nonexistent-path` (or any invalid path) to make those calls fail in <300ms instead.

## Frontend / Templates

- **Never use inline styles** — all styling goes in `base.html` `<style>` block as CSS classes.
- Use `.btn`, `.btn-sm`, `.btn-green`, `.btn-outline`, `.action-bar` for buttons and action groups.
- Errors must always be visible to the user — every form/endpoint must surface error messages.
- All form submissions must show a loading spinner — handled globally in `base.html` JS.
