# Changelog

All notable changes to **AgentIT** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for tagged releases. Until `v0.1` is cut, unreleased work lands under **[Unreleased]**.

Dense dogfood / session prose formerly in the README lives in [`docs/history/`](docs/history/).
Product contract detail: [`docs/release-notes.md`](docs/release-notes.md).

## [Unreleased]

### Added

- Product-legibility docs: scannable README, [`docs/score-methodology.md`](docs/score-methodology.md), [`docs/compare.md`](docs/compare.md), [`docs/adr/`](docs/adr/), [`docs/history/`](docs/history/).
- Portal crisp IA (PR [#160](https://github.com/alimobrem/AgentIT/pull/160)): fixed masthead + footer; denser Assessment / Capabilities / Health / Insights / Fleet surfaces.
- Checks & resolutions catalog on Capabilities (`portal/check_catalog.py`, PR [#159](https://github.com/alimobrem/AgentIT/pull/159)).
- Solution contracts so Scan PRs clear findings (`SOLUTION_CONTRACTS`, clear-evidence simulation; PRs [#154](https://github.com/alimobrem/AgentIT/pull/154), [#158](https://github.com/alimobrem/AgentIT/pull/158)).

### Changed

- README is the product front door (~100–150 lines); history and contracts moved out.
- Scan HITL + GitOps-only delivery is the documented operate path (see [ADR 0001](docs/adr/0001-gitops-scan-hitl.md)).
- Postgres is the only assessment store (see [ADR 0002](docs/adr/0002-postgres-store.md)).

### Deferred (tracked, not half-built)

See [`docs/history/backlog.md`](docs/history/backlog.md): portal screenshots / demo GIF, GitHub Release `v0.1`, shareable score badge, hosted/podman demo one-liner, full portal nav regroup, competitive one-pager expansion. CLI `--no-llm` already exists and is surfaced in the README / score methodology.

## [0.1.0] — TBD

First tagged release planned after product-legibility + crisp portal land. Placeholder until a GitHub Release is cut.

---

## Earlier history (pre-changelog)

Skills-primary Scan, quality PR Phases A–F, HPA gates, ApplicationSet recurse, clearable findings, audit wiring, Postgres migration, and prior delivery-model narratives are preserved as dated notes under [`docs/history/`](docs/history/) (start with [`changelog-dogfood-notes.md`](docs/history/changelog-dogfood-notes.md)). Do not treat those files as current product truth.
