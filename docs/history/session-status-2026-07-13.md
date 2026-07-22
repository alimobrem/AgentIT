# Session Status — 2026-07-13 (night session)

Snapshot written mid-session as a handoff artifact, since this conversation has run long enough that a fresh session is the right way to continue. Grounded in `git log origin/main` at the time of writing, not memory — verify against current `git log` before trusting this if much time has passed.

## Where things stand

`origin/main` HEAD as of writing: `877df9a`. Everything below is pushed and live (Argo CD auto-syncs `main`).

## Shipped tonight, newest first

- `877df9a` — Added YAML template fallbacks for the 8 previously LLM-only skills, so they produce a valid baseline artifact even without an LLM connection. Also closed the chaos-skill coverage gap. Includes parity tests for skill-vs-agent output and an `docs/agent-removal-readiness.md` checklist (see below).
- `32a331d` — HA Postgres chart prep (CloudNativePG-based, gated off by default) + a concrete async migration plan doc for `store.py`. **Does not touch `store.py` or any of its ~15+ callers** — that rewrite is intentionally deferred (see "Deferred" below).
- `f050255` — 6 concrete runtime bugs: onboarding summary parity (`auto_approve`/`gates` now consistent between UI and webhook paths), SLO tracker actually collects live metrics now (it didn't before), SLO breach direction fixed for higher-is-better metrics, API-drift-detector `AttributeError` fixed (was silently swallowed every tick), `kube.py` cluster API discovery now covers all API groups (was core `v1` only — this was silently breaking platform-aware skill gating), real Argo Rollouts rollback implemented (was just a restart annotation), and the `--rescan` CLI flag that CronJobs were already calling now actually exists.
- `d62e83c` — Fixed CodeChangeAgent's category/handler mismatch (some finding categories were silently producing nothing); clarified Compliance agent's Kyverno `Policy` (namespaced) vs `ClusterPolicy` scope claim.
- `fb4cbbe` — Unified workload labels/kind across infrastructure/cicd/release agents (HPA/PDB were targeting the wrong object due to a label mismatch); CI/CD pipeline's image-scan/sbom-generate steps are now conditional on those tasks actually being generated (previously could reference tasks that don't exist).
- `9bdce3c` — Closed the Argo CD `Application` self-sync gap that caused tonight's stale-image incident (the live `Application` object's own Helm parameters weren't auto-syncing from git); tuned `build-image` task resources.
- `c411461` — Migrated most `oc`/`kubectl` subprocess call sites (`image_builder.py`, `health.py`, `github_pr.py`) to the Kubernetes Python API client via `kube.py`, closing the architectural gap that caused the test-suite-pollutes-live-cluster incident earlier tonight. `kube.py`'s own `apply_yaml` may still be subprocess-based — check the commit/file directly for current status, it was flagged as the highest-risk piece to migrate.
- `756fbc5` — Orchestrator now actually passes an LLM client to the skill engine (it never did before — skills-first generation was template-only in production despite LLM credentials being configured), and conflict detection now only flags real conflicts instead of "any two agents both succeeded" (which made auto-approve practically unreachable).
- `88a879d` — Fixed naive pluralization in the skill engine's platform-kind gate (`NetworkPolicy` → was checking `networkpolicys`, should be `networkpolicies` — this was silently skipping skills on real clusters).
- `5152b78` — New skill/check catalog change tracking: additions and removals of skills/checks are now diffed hourly and logged as real events, visible on `/events` and a new "Recent Catalog Changes" section on `/capabilities`.
- `e29009e` — Fixed a real 504 on `/capabilities/learn` (OpenShift Route had no timeout override, defaulted to 30s; the route's own work can take up to 180s).
- `9c11826` — Enabled the `skill-learner` watcher (built earlier tonight) on the live deployment — confirmed running, already drafted 3 real CVE skills on its first cycle.
- `c274055` — Nav IA cleanup: Agents/Capabilities and Schedules/Settings paired as tabs, top-level nav down from 9 items to 7.
- `e2cc0fe` and earlier — Closed the "Learn" loop end-to-end in the portal (research button + automatic watcher + Activate button for draft skills), fixed 3 live UX bugs (broken Delete button, empty skills/checks catalog in production, 0%-success-rate display bug on Insights), RBAC fix for Argo CD visibility, test isolation fix (tests were polluting the live cluster).

## Still possibly in flight

- A fix for a reported "Apply to Cluster button does nothing" bug — dispatched, no confirmed landing commit yet as of this doc. Check `git log` for anything mentioning "apply" past `877df9a`, or check subagent `e61b0b43` if resuming this exact session.

## Deliberately deferred — not started, by design

These need a **fresh, focused session**, not to be bolted onto more parallel work:

1. **Full removal of the hardcoded Python agents** (`src/agentit/agents/*.py`), in favor of the LLM-based skill engine. The product owner has decided to do this despite an earlier recommendation to keep a hybrid model. Prep work is done (`877df9a`'s skill templates + `docs/agent-removal-readiness.md`) — the actual cutover (removing agent code, updating the orchestrator's skip logic, removing now-dead tests) was intentionally not started tonight because it depends on and would have conflicted with several of the parallel fixes above. **Read `docs/agent-removal-readiness.md` first** — it has the real coverage-gap analysis (some agent outputs, like CodeChangeAgent's source patches and the cost/dependency narrative reports, may not fit the skill model at all).
2. **Migrating `store.py` (SQLite) to async Postgres.** Design work is done (`32a331d`'s `docs/postgres-migration-plan.md`, which includes a real call-site inventory — read this to scope the work honestly). The actual rewrite of `store.py` and its ~15+ callers was not started, deliberately — `store.py` was the most contended file of the whole session tonight (3+ workers touching it), and doing this rewrite needed a clean, uncontested pass.

## Known architecture issues raised tonight, not yet acted on

From an architecture review earlier in the session (not yet started as work): shared SQLite over one ReadWriteOnce PVC across multiple Deployments (addressed by the deferred Postgres migration above), **no authentication on any portal route** (still true — this is probably the single highest-risk open item if this app is ever exposed beyond a controlled demo), and the dual human+LLM gate design has some structural inconsistencies beyond tonight's conflict-detection fix.

## Operational notes for whoever picks this up

- Argo CD (`agentit` app, `openshift-gitops` namespace) auto-syncs `main` with `selfHeal: true`. Never `helm upgrade`/manual `oc apply` the app itself — commit and push, Argo CD does the rest. The one exception found tonight: `argocd/application.yaml`'s own Helm-parameter changes needed a manual `oc apply` of that file specifically until `9bdce3c` closed that gap — verify that fix actually holds before assuming parameter changes are now fully automatic.
- `argocd/application.yaml` still commits `image.tag: latest` as a bootstrap/disaster-recovery placeholder; the real live value is patched directly by the CI pipeline's `notify-argocd` step to the exact deployed commit SHA. Don't be alarmed seeing `latest` in the git-committed file — that's expected. Re-applying that file raw will reset the live pin; re-check `9bdce3c`'s fix handles this correctly before doing so.
- LLM connectivity: Vertex AI via `itpc-gcp-product-all-claude` project, working as of tonight (confirmed both locally and via the live skill-learner watcher's first successful run).
