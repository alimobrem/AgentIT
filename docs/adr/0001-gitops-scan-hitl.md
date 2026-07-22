# ADR 0001: GitOps-only Scan HITL

- **Status:** Accepted
- **Date:** 2026-07
- **Brand:** AgentIT

## Context

AgentIT must get remediations onto clusters without silent live mutation and without auto-merging pull requests.

## Decision

1. **Scan** (onboard / `auto_delivery`) is the only path that opens remediation PRs.
2. Delivery always stops at a **GitHub PR**. A human merges; **Argo CD** applies desired state.
3. Preflight uses apiserver **SSA dry-run** (`kube.apply_yaml(..., dry_run=True)`), not a live apply disguised as preview.
4. There is no Direct Apply from the portal Deliver path and no auto-merge of AgentIT-opened PRs.

## Consequences

- Fleet apps: PRs under `apps/{app}/` + ApplicationSet recurse.
- Self-managed AgentIT: PRs on AgentIT.git (`chart/`, `skills/`, `src/`), never `apps/agentit/` in gitops.
- Portal Ledger / GitHub are the human approval surfaces.

## See also

- [`../release-notes.md`](../release-notes.md)
- [`../architecture-agentit-vs-fleet-gitops.md`](../architecture-agentit-vs-fleet-gitops.md)
