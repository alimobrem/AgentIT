# AgentIT Helm chart

Deploys AgentIT on OpenShift under Application **`agentit`** (see `argocd/application.yaml`). Argo CD is the sole deployer — do not `helm upgrade` by hand.

## Product context

- Self-managed: this chart + image promote via Tekton `notify-argocd`.
- Fleet apps are **not** rendered from this chart; they sync from gitops `apps/{app}/` via ApplicationSet (`recurse` YAML). Never put AgentIT desired state under `apps/agentit/`.
- See [docs/architecture-agentit-vs-fleet-gitops.md](../docs/architecture-agentit-vs-fleet-gitops.md) and [docs/deployment.md](../docs/deployment.md).

## Values

Feature flags live in `values.yaml` (watchers, Kafka, Tekton CI, auth, bundled Postgres, …). The live cluster overrides via `argocd/application.yaml` Helm parameters — chart defaults stay quiet for fresh installs.

## Templates of note

| Path | Role |
| --- | --- |
| `templates/deployment.yaml` / Rollout | Portal |
| `templates/tekton/` | `agentit-ci` Pipeline + Triggers |
| `templates/agents/` | Watcher Deployments |
| `templates/postgres/` | Bundled Postgres (only supported store) |
