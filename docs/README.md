# AgentIT documentation

**Canonical product truth:** [../README.md](../README.md) and [architecture.md](./architecture.md) (merged `main`, post-dogfood Scan HITL).

## Start here

| Doc | Role |
| --- | --- |
| [../README.md](../README.md) | Product contract, architecture at a glance, setup |
| [architecture.md](./architecture.md) | System diagrams, Scan pipeline, obsolete paths |
| [architecture-agentit-vs-fleet-gitops.md](./architecture-agentit-vs-fleet-gitops.md) | **Normative** self-managed vs fleet delivery |
| [deployment.md](./deployment.md) | OpenShift / Argo / Tekton ops runbook |
| [plan-quality-helpful-prs.md](./plan-quality-helpful-prs.md) | Quality PR Phases A–F (implemented) |
| [portal-experience-design-language.md](./portal-experience-design-language.md) | Portal EDL |

## Current product (short)

- Skills-primary; Scan/`auto_delivery` is the only PR creator
- No Direct Apply, no Per-Agent PRs, no AutoMode auto-merge
- SSA dry-run preflight; humans merge; Argo deploys
- Fleet: `apps/{app}/` + AppSet recurse; AgentIT: Helm `chart/` (never `apps/agentit/`)

**Solution contracts:** [#154](https://github.com/alimobrem/AgentIT/pull/154) landed on `main`. Hardening (detect_only coverage, clear-evidence simulation, skill↔contract CI, fleet/self-managed path hints, PR-card honesty) ships in the contract-hardening PR — see README **Solution contracts**.

## Historical / planning (do not treat as live product)

| Doc | Status |
| --- | --- |
| [unified-apply-flow.md](./unified-apply-flow.md) | **Historical** — Direct Apply / AutoMode era design |
| [onboarding-loop-vision-gap-analysis.md](./onboarding-loop-vision-gap-analysis.md) | Planning record; much superseded |
| [changelog-dogfood-notes.md](./changelog-dogfood-notes.md) | Session notes archived from README |
| [agent-removal-readiness.md](./agent-removal-readiness.md) | Pre–skills-primary audit; agents mostly gone |
| [postgres-migration-plan.md](./postgres-migration-plan.md) | Migration complete |
| [dogfood-self-improve-milestone.md](./dogfood-self-improve-milestone.md) | Milestone notes |
| [extension-model-unification-*.md](./extension-model-unification-plan-2026-07-18.md) | Unification design; Phases 1–5 shipped |
| [ui-redesign-proposal.md](./ui-redesign-proposal.md), [next-gen-ux-concepts.md](./next-gen-ux-concepts.md), [ux-design-requirements.md](./ux-design-requirements.md) | UX proposals — verify against live portal |
| [ledger-design-spec.md](./ledger-design-spec.md) | Ledger design; gates table removed since |
| [kafka-hardening-plan.md](./kafka-hardening-plan.md) | Deferred hardening |
| [self-improvement-for-agentit.md](./self-improvement-for-agentit.md) | capability-scout design (implemented core) |

## Other

| Doc | Role |
| --- | --- |
| [agentit-pr-types-quality-review.md](./agentit-pr-types-quality-review.md) | PR-types quality inventory |
| [portal-crawl-matrix.md](./portal-crawl-matrix.md) | Portal crawl coverage |
| [resilience-audit-2026-07-18.md](./resilience-audit-2026-07-18.md) | Resilience audit |
| [cicd-stall-hardening-2026-07-17.md](./cicd-stall-hardening-2026-07-17.md) | Tekton stall incident |
| [self-health-check-backlog.md](./self-health-check-backlog.md) | Self-health watcher backlog |
| [capabilities-ux-redesign-notes.md](./capabilities-ux-redesign-notes.md) | Capabilities UX notes |
