# Changelog

All notable changes to **AgentIT** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for tagged releases. Until `v0.1` is cut, unreleased work lands under **[Unreleased]**.

Dense dogfood / session prose formerly in the README lives in [`docs/history/`](docs/history/).
Product contract detail: [`docs/release-notes.md`](docs/release-notes.md).

## [Unreleased]

### Added

- **Secret-classify dedup:** quick-win heuristics for `__PLACEHOLDER__` tokens and Prometheus/alert `secret="…"` *name* labels (no LLM / no Decisions rows); durable `secret_classify_cache` keyed by `(app, path, snippet_hash)` so repeat Scans skip LLM + event logging on confident drops (log only first sight or outcome flip).
- Merge-gate + post-merge deploy tip docs: [`docs/ci-deploy.md`](docs/ci-deploy.md) + [`scripts/ci-merge-gate.sh`](scripts/ci-merge-gate.sh) (do not merge on queued checks; verify GHA + `agentit-ci/tekton` + rollout tip).
- **Phase 3 hardening**: interfaces layer (`agentit.interfaces`), full decision card (why · confidence · dry-run · evidence · approve/reject), Fleet/Ledger card-collapse at 375px, ADRs 0005–0007.
- **Score model v2**: pass-ratio dimensions + criticality-weighted overall; letter grades; SVG badge at `/badge/{app}.svg` ([ADR 0003](docs/adr/0003-score-model-v2.md)).
- **RepoSnapshot** single-pass tree read + concurrent analyzers (assessment latency).
- Score-first Assessment Detail with top-3 estimated fix impacts; centralized score bands.
- Guided first-run: empty fleet → `/fleet`; else `/` → Ledger. Nav spine Fleet → Ledger; Operate under Menu.
- Checked-in sample assessment: [`examples/sample-assessment.md`](examples/sample-assessment.md).
- Fail-closed webhook auth unless `AGENTIT_ALLOW_UNVERIFIED_WEBHOOKS=1`.
- Product-legibility docs: scannable README, [`docs/score-methodology.md`](docs/score-methodology.md), [`docs/compare.md`](docs/compare.md), [`docs/adr/`](docs/adr/), [`docs/history/`](docs/history/).
- Portal crisp IA (PR [#160](https://github.com/alimobrem/AgentIT/pull/160)): fixed masthead + footer; denser Assessment / Capabilities / Health / Insights / Fleet surfaces.
- Checks & resolutions catalog on Capabilities (`portal/check_catalog.py`, PR [#159](https://github.com/alimobrem/AgentIT/pull/159)).
- Solution contracts so Scan PRs clear findings (`SOLUTION_CONTRACTS`, clear-evidence simulation; PRs [#154](https://github.com/alimobrem/AgentIT/pull/154), [#158](https://github.com/alimobrem/AgentIT/pull/158)).
- **Image signing good-PR path:** detect `image-signing-exists` (`file_contains: cosign`) → remediable `image_signing` contract → `cosign-sign-task` (keyless Sigstore Tekton Task). Clear-evidence `cosign_sign_task` refuses empty Task / SLSA L3 / hermetic / Konflux theater without `cosign sign`/`attest`. Optional: pin Syft on `sbom-task` to `v1.48.0` (stop `:latest`).

### Fixed
- **Shallow-PR clear-evidence harden (skills audit):** dedicated evidence kinds
  replace bare `cluster_kind` for the top shallow Scan skills —
  `image_scan_task` (trivy/grype/snyk + refuse empty Task / `:latest` step
  images), `grafana_dashboard` (label + non-empty panels), `selector_target`
  (PDB/ServiceMonitor must match live Services/workloads; refuse zero-match),
  `argocd_application` (repoURL + path/chart; refuse bogus `deploy/` when tree
  missing). `migration_tooling` refuses `SELECT 1`, empty `upgrade()`/`pass`,
  and comment-only `op.execute` (require real DDL). Generators/templates pinned
  accordingly. Same refuse class as SBOM empty shells (#199).
- **SBOM inventory (not empty shells):** `sbom-artifact` populates CycloneDX
  `components` via Syft when available, else lockfiles/manifests
  (`requirements.txt`, `package.json`, `go.mod`, …). Delivery enrichment runs
  before clear-evidence; `sbom_file` refuses `components: []` theater
  (pulse-agent#3 class). Tip after merge: next Scan opens a real BOM.
- **Portal OOM under concurrent GitHub webhooks (dogfood):** clone+assess had no concurrency bound and the Rollout sat at 512Mi; overlapping push reassesses OOMKilled the pod mid-run (pinky → webhook 504, push-driven finding verification never ran). Raise portal memory to 1Gi, serialize in-process assess (default max 1 via `assessConcurrency` / `AGENTIT_ASSESS_MAX_CONCURRENT`), and fail soft with HTTP 503 + claim release so GitHub can redeliver the same `X-GitHub-Delivery`.
- **SBOM good-PR path:** compliance `sbom` clears via source CycloneDX
  (`skills/compliance/sbom-artifact.md`, clear-evidence `sbom_file`) — not a
  cluster Tekton `sbom-task` that never satisfied Assess's app-repo file check.
- **Schedules page (dogfood):** multi-document onboarding `*-cronjob.yaml` bundles (SA/Role before CronJob) rendered every row as `unresolvable` because `yaml.safe_load` only read the first doc; Containerfile omitted `watchers/` + `agents/` registration Markdown so `WATCHER_AGENTS` was empty in-cluster (“0 Long-Lived Agents”); page did not show per-app `assessment_cadence` or live platform CronJobs, so empty/unresolvable onboarding tables looked like “nothing has a schedule.” Parse all YAML docs, ship registration dirs, surface cadence + `list_cronjobs`, and clarify empty-state copy.
- **Skill learning (minimal ship):** clear-evidence theater refusals now record `skill_effectiveness` rejects (same path as post-merge still-present; deduped per skill+reason within one delivery). After **2** identical reject reason prefixes for `(app, skill)`, that skill cools down and is skipped in `match()` / Fix dispatch; unchanged failure reasons skip blind `redispatch_finding_fix()` (escalate + log `skill-learner-queued`). `run_all()` rejection skip uses finding **category** (not `skill.domain`). skill-learner fast-path flags at the same **2** identical-reject threshold. Capabilities surfaces cooling-down skills.
- **capability-scout tests-pass (dogfood):** chart wires a dedicated throwaway Postgres sidecar + `AGENTIT_TEST_PG_DSN` (same pattern as Tekton `run-tests` / GHA services) so the in-cluster gate can execute real pytest instead of 0 passed / thousands skipped. Never the fleet bundled DB (fixtures `TRUNCATE`). All-skip infra failures no longer stick `fix_regression_only=true`. Values: `agents.capabilityScout.testPostgres.*` (optional external `dsn` for dogfood Argo).
- **Decisions fix-review flood (dogfood):** post-merge `still_present` / PR-close paths recorded `skill_effectiveness` rejects for every companion skill YAML on the assessment (pdb/limitrange/…) when a source-patch migration/container delivery failed clear-evidence — not LLM fix-review. Attribute approve/reject to the finding’s `SOLUTION_CONTRACT` skill only.
- **Audit wiring (dogfood):** clear-evidence `audit_wired` ran *before* delivery relocated root `audit.py` into the app package — Fix/Scan refused with “audit module at repo root only” even when enrichment would have cleared (pinky). Pre-enrich audit (+ pin-only Containerfile) before simulation; drop orphan stubs when the default branch is already packaged+wired; compliance requires a **packaged** audit module path (not repo-root theater). Skill/SOLUTION_CONTRACT document the package + import/call-site bar.
- Pin AgentIT `Containerfile` base to `ubi9/python-312@sha256:89ef0dda…` (immutable digest; closes theater `:latest`→`:1` Scan PR [#173](https://github.com/alimobrem/AgentIT/pull/173)).

- Scan **container** remediation is **pin-only** on existing Dockerfile/Containerfile (FROM `:latest` → `:1` / digest); clear-evidence refuses destructive stub rewrites (#165 class; same bar as migration [#163](https://github.com/alimobrem/AgentIT/pull/163)).
- Tekton `agentit-ci` `run-tests` timeout raised to **20m** (was 10m) so UBI pip+pytest under node pressure / one retry does not TaskRunTimeout before image pin (tip `b4ae400f` / `agentit-ci-bwb76`).
- Align hermetic tests with PR [#161](https://github.com/alimobrem/AgentIT/pull/161): suite-wide `AGENTIT_ALLOW_UNVERIFIED_WEBHOOKS=1`, score v2 expectations, first-run `/` → `/fleet`, Assessment Detail status-strip copy.
- Scan **migration** finding: detect hand-rolled idempotent store DDL (AgentIT ADR 0002 / `SCHEMA_SQL`) so dogfood does not open stub Alembic PRs; clear-evidence refuses `target_metadata = None` theater; `db-migration-tooling` emits a real first revision + env URL wiring (closes the #157 class of useless PRs).
- Post–Phase 3 CI: `LLMClient._chat` fail-soft on unexpected errors again (credentials / bare SDK failures); Fleet design-system test asserts `fleet-table` class.

### Changed

- Portal footer is an action-feedback status strip (no duplicated nav links); toasts mirror into it.
- Clone SSRF: HTTPS-only; resolve + re-resolve before clone; TLS blocks IP-pin ([ADR 0005](docs/adr/0005-ssrf-clone.md)).
- Circuit breakers and score aggregation moved to `agentit.interfaces` ([ADR 0006](docs/adr/0006-interfaces-layer.md)); portal re-exports breakers.
- README is the product front door (~100–150 lines); history and contracts moved out.
- Scan HITL + GitOps-only delivery is the documented operate path (see [ADR 0001](docs/adr/0001-gitops-scan-hitl.md)).
- Postgres is the only assessment store (see [ADR 0002](docs/adr/0002-postgres-store.md)).

### Deferred (tracked, not half-built)

See [`docs/history/backlog.md`](docs/history/backlog.md): portal screenshots / demo GIF, GitHub Release `v0.1`, hosted/podman demo one-liner, full Events∪Decisions merge, competitive one-pager expansion.

## [0.1.0] — TBD

First tagged release planned after product-legibility + crisp portal land. Placeholder until a GitHub Release is cut.

---

## Earlier history (pre-changelog)

Skills-primary Scan, quality PR Phases A–F, HPA gates, ApplicationSet recurse, clearable findings, audit wiring, Postgres migration, and prior delivery-model narratives are preserved as dated notes under [`docs/history/`](docs/history/) (start with [`changelog-dogfood-notes.md`](docs/history/changelog-dogfood-notes.md)). Do not treat those files as current product truth.
