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
3. The push triggers the Tekton CI pipeline; its `notify-argocd` task runs
   `oc apply -f argocd/application.yaml` as its first step (before re-pinning
   `image.tag`), so the live `Application` object's `spec.source.helm.parameters`
   picks up the change automatically as part of the normal deploy flow — no
   manual `oc apply` needed.
4. If you need it to land *before* the next CI run (e.g. no code change, so no
   pipeline would otherwise trigger), Argo CD's own automated sync only reverts
   drift in the resources it renders — it does **not** watch this file for
   changes to the `Application` object's own spec. Apply it yourself once:
   `oc apply -f argocd/application.yaml -n openshift-gitops`. This is safe: it
   will not disturb `image.tag`, because CI's `update-image-tag` step always
   runs immediately after `sync-application-spec` on every pipeline run and
   re-pins it to the last successfully built commit SHA.

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

## Operator prerequisites

Some optional chart features assume their operator is already installed cluster-wide via OLM — the chart only ships the Custom Resource, not the operator itself. This mirrors how Argo CD/Argo Rollouts are assumed to already be running.

### Kafka (`kafka.enabled: true`)

Requires the **Strimzi** operator (`strimzi-kafka-operator`) installed via a `Subscription` in `openshift-operators` (or its own namespace). The chart renders `Kafka`/`KafkaNodePool` CRs (`chart/templates/kafka/`) that Strimzi reconciles.

### Postgres (`postgres.enabled: true`)

Requires the **CloudNativePG** operator installed via a `Subscription` (Red Hat-certified, available on OperatorHub as `cloud-native-postgresql`):

```bash
oc get packagemanifests -n openshift-marketplace cloud-native-postgresql
```

```yaml
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: cloud-native-postgresql
  namespace: openshift-operators
spec:
  channel: stable
  name: cloud-native-postgresql
  source: certified-operators
  sourceNamespace: openshift-marketplace
```

Once installed, the chart renders a 3-instance HA `Cluster` CR (`chart/templates/postgres/postgres-cluster.yaml`) plus an app-credentials `Secret` (`chart/templates/postgres/postgres-secret.yaml`). See [`docs/postgres-migration-plan.md`](postgres-migration-plan.md) for why CloudNativePG was chosen and the plan for actually cutting `store.py` over to it — as of this writing the chart resources exist but the app still talks to SQLite (`postgres.enabled` defaults to `false`).

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

### A new Helm parameter in `argocd/application.yaml` isn't showing up on the live `Application`
This is expected until the next CI run (or a manual `oc apply`, see above) — Argo CD's automated
sync/self-heal only reconciles the resources *rendered by* the `Application`'s live spec against
the cluster; it does not reconcile the `Application` object's own spec against git. There is no
app-of-apps/bootstrap controller managing `argocd/application.yaml` itself (deliberately — the
sibling `agentit-managed-apps` ApplicationSet in `openshift-gitops` explicitly excludes
`apps/agentit`, to avoid its own selfHeal fighting with CI's live `image.tag` patch on every
reconcile). `notify-argocd`'s `sync-application-spec` step covers this instead.

### LLM not working in container
Check: `oc exec <pod> -- env | grep GOOGLE` — needs `GOOGLE_APPLICATION_CREDENTIALS` pointing to a mounted secret.
