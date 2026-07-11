# AgentIT Deployment Guide

## Architecture

AgentIT is deployed via **Argo CD GitOps**. Argo CD is the sole deployer — never run `helm upgrade` manually.

```
Git push → Argo CD detects change → Renders Helm chart → Applies to cluster → Argo Rollouts manages canary
```

## How to Change Configuration

### Change a Helm value (e.g., replica count, feature flag)

1. Edit `argocd/application.yaml` — add or change a parameter
2. `git commit && git push`
3. Argo CD auto-syncs within ~3 minutes (or force: `oc annotate app agentit -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite`)

### Change a secret (never in Git)

1. Create or update the secret on-cluster:
   ```bash
   oc create secret generic gcp-credentials -n agentit \
     --from-file=credentials.json=/path/to/credentials.json \
     --dry-run=client -o yaml | oc apply -f -
   ```
2. Reference it in `argocd/application.yaml` via a Helm parameter:
   ```yaml
   - name: gcp.credentialsSecret
     value: gcp-credentials
   ```
3. Push and let Argo CD sync.

## DO NOT

- **Never run `helm upgrade` manually** — it conflicts with Argo CD's server-side apply
- **Never `oc edit` the Rollout** — Argo CD will revert it
- **Never put credentials in `values.yaml`** — the repo is public
- **Never delete Services manually** — the Rollouts controller manages them

## Troubleshooting

### "conflict with rollouts-controller on .spec.selector"
You ran `helm upgrade` manually. Stop. Let Argo CD manage it.

### Env vars not appearing in pods
Check `argocd/application.yaml` has the parameter. Push. Force refresh.

### Argo CD not syncing
Force: `oc annotate app agentit -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite`

### LLM not working in container
Check: `oc exec <pod> -- env | grep GOOGLE` — needs `GOOGLE_APPLICATION_CREDENTIALS` pointing to a mounted secret.
