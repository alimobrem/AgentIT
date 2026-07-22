# AgentIT — short differentiators

Stub for a fuller competitive one-pager (deferred: [`history/backlog.md`](./history/backlog.md)). Facts only from current AgentIT docs/code.

| Differentiator | What AgentIT does |
| --- | --- |
| **Score-first CLI** | `agentit assess <repo>` scores seven enterprise dimensions without a cluster |
| **Findings → quality PRs** | Scan opens finding-tied GitOps/source PRs with SSA dry-run + clear-evidence gates — not catalog dumps |
| **Human merge, GitOps deploy** | No portal Direct Apply; Argo CD applies after merge ([ADR 0001](./adr/0001-gitops-scan-hitl.md)) |
| **Skills + detect checks** | Property-based skills generate remediations; `mode: detect` skills contribute findings |
| **OpenShift-native operate loop** | Charted for Argo CD, Rollouts, Tekton; plain K8s assess works, full stack assumes OpenShift |

Not a feature checklist against named vendors — expand only with verified sources.
