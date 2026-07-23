# AgentIT documentation

**Canonical product truth:** [../README.md](../README.md), [architecture.md](./architecture.md), [score-methodology.md](./score-methodology.md).

Brand spelling: **AgentIT** (not AgentIt / Agentit).

## Start here

| Doc | Role |
| --- | --- |
| [../README.md](../README.md) | Product front door — score path, core loop, quick start, dogfood screenshots under [`assets/readme/`](./assets/readme/) |
| [score-methodology.md](./score-methodology.md) | 7 dimensions, penalties, overall average, PR impact |
| [release-notes.md](./release-notes.md) | Product contract, solution contracts, portal IA |
| [../CHANGELOG.md](../CHANGELOG.md) | Keep a Changelog history |
| [architecture.md](./architecture.md) | System diagrams, Scan pipeline |
| [architecture-agentit-vs-fleet-gitops.md](./architecture-agentit-vs-fleet-gitops.md) | Self-managed vs fleet delivery |
| [deployment.md](./deployment.md) | OpenShift / Argo / Tekton ops |
| [plan-quality-helpful-prs.md](./plan-quality-helpful-prs.md) | Quality PR rules (implemented) |
| [portal-experience-design-language.md](./portal-experience-design-language.md) | Portal EDL |
| [compare.md](./compare.md) | Short differentiators |
| [adr/](./adr/) | Architecture Decision Records |

## Current product (short)

- Score any repo with `agentit assess` (no cluster)
- Skills-primary generation; Scan opens quality-filtered PRs
- Human merge → Argo CD deploy (GitOps-only Scan HITL)
- Postgres store for portal / fleet ([ADR 0002](./adr/0002-postgres-store.md))
- Fleet: `apps/{app}/` + AppSet recurse; AgentIT: Helm `chart/`

**Live catalog:** Capabilities → Checks & resolutions (`portal/check_catalog.py`). Detail: [release-notes.md](./release-notes.md#solution-contracts).

## Historical (not live product)

Session notes, phase dumps, audits, superseded designs: **[history/](./history/)**. Deferred legibility work: [history/backlog.md](./history/backlog.md).

| Still at `docs/` root (reference) | Role |
| --- | --- |
| [extension-model-unification-spec.md](./extension-model-unification-spec.md) | Extension model design spec (plans → history) |
| [portal-crawl-matrix.md](./portal-crawl-matrix.md) | Portal crawl coverage |
