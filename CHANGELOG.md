# Changelog

All notable changes to **AgentIT** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for tagged releases. Until `v0.1` is cut, unreleased work lands under **[Unreleased]**.

Dense dogfood / session prose formerly in the README lives in [`docs/history/`](docs/history/).
Product contract detail: [`docs/release-notes.md`](docs/release-notes.md).

## [Unreleased]

### Added

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

### Fixed
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
