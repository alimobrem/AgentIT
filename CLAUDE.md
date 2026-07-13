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
- **`FleetOrchestrator`, `AutoMode`, `RemediationDispatcher`, and `RemediationLoop` are permanently synchronous — do not make them `async def`.** They're each either a long-running, potentially-blocking pipeline (`FleetOrchestrator.run()` spawns K8s Jobs and polls) or a background-thread job runner (`RemediationLoop.start()`), and `asyncpg` connection pools are not safe to use across threads/from inside a non-event-loop context. Any caller reaching one of these four from async code must: resolve the store first (`s = await get_store()`), extract the synchronous handle (`raw = s.raw if hasattr(s, "raw") else None` — `None` means the postgres backend, which isn't supported for these four yet; fail loudly, don't guess), construct the class with `raw`, and run the actual call via `asyncio.to_thread(...)` so it doesn't block the event loop. See `docs/postgres-migration-plan.md`'s "Progress update: Phase 3 complete" section for the full rationale.
- **`store_pg.AssessmentStore` has no `.raw`, on purpose.** Handing a not-yet-async-converted synchronous consumer (the four classes above, or a background thread) a Postgres-backed store is exactly the kind of silent partial-cutover this codebase's Postgres migration plan (`docs/postgres-migration-plan.md` §7) warns against — that combination must fail loudly (e.g. `AttributeError`/an explicit check-and-raise), never silently degrade.
- **Never construct a second, separate `AsyncSQLiteStore(":memory:")` in tests that need to share data with an existing sync `AssessmentStore(":memory:")`** — each `:memory:` connection is its own isolated database. Use `AsyncSQLiteStore.wrap(existing_sync_store)` (in `store_factory.py`) to get an async-compatible facade over the *same* connection instead.

## Testing

- On a machine with a real (even if currently unreachable) `~/.kube/config`, `pytest` can hang for minutes: fleet/health routes call `kube.list_custom_resources(...)` unconditionally (by design — it's a `try`/`except`-wrapped resilience feature, not something to gate behind `--live-cluster`), and the Kubernetes client's `_request_timeout` doesn't bound DNS/TCP-connect time. Run tests with `KUBECONFIG=/tmp/nonexistent-path` (or any invalid path) to make those calls fail in <300ms instead.

## Frontend / Templates

- **Never use inline styles** — all styling goes in `base.html` `<style>` block as CSS classes.
- Use `.btn`, `.btn-sm`, `.btn-green`, `.btn-outline`, `.action-bar` for buttons and action groups.
- Errors must always be visible to the user — every form/endpoint must surface error messages.
- All form submissions must show a loading spinner — handled globally in `base.html` JS.
