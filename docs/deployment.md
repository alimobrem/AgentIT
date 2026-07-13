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

**Current state is single-broker, plaintext, no auth** — any pod in the namespace can produce/consume on any topic. See [`docs/kafka-hardening-plan.md`](kafka-hardening-plan.md) for the deferred plan to add TLS + SASL/SCRAM auth via Strimzi `KafkaUser` CRs and wire credentials into `events.py`/`consumer.py`.

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

## Authentication

The portal had zero authentication, CSRF protection, or API authentication until this section's features landed. Three independent layers, each addressing a different caller:

| Caller | Mechanism | Where |
|---|---|---|
| Browser users hitting the Route | OpenShift `oauth-proxy` sidecar (`auth.enabled`) | `chart/templates/deployment.yaml`, `route.yaml`, `service.yaml` |
| Browser forms/htmx POSTs | Double-submit-cookie CSRF | `src/agentit/portal/csrf.py`, `app.py`'s `csrf_middleware` |
| Argo Events Sensors calling `/api/webhook/*` | Shared-secret bearer token | `src/agentit/portal/routes/webhooks.py`'s `verify_internal_token` |

### `auth.enabled` (oauth-proxy sidecar)

Defaults to `false` so merging this doesn't change behavior on any existing deployment. To turn it on:

1. Make sure the Route name matches what the SA's `oauth-redirectreference` annotation expects (it's `{{ .Release.Name }}`, i.e. `agentit` by default — no action needed unless you rename the release).
2. Set the Helm parameter: `auth.enabled: true` (via `argocd/application.yaml`, same pattern as any other flag — see "Change a Helm value" above).
3. Push. On the next sync, the Deployment gains an `oauth-proxy` sidecar (`registry.redhat.io/openshift4/ose-oauth-proxy`) listening on 8443, the Service gains a `proxy-https` port plus a `service.beta.openshift.io/serving-cert-secret-name` annotation (the service-ca operator mints the sidecar's TLS cert automatically — no manual cert management needed), and the Route switches from `edge` termination on port 8080 straight to `reencrypt` termination on the proxy's port.
4. The SA also gets a `system:auth-delegator` ClusterRoleBinding (needed for the proxy's `--openshift-sar` check) and an `oauth-redirectreference` annotation (needed for the OAuth login flow's redirect).

**Prerequisite**: none beyond what every OpenShift cluster already has — `oauth-proxy` talks to the cluster's built-in OAuth server, there's no separate IdP to stand up. `--openshift-sar` is currently scoped to "can this identity `get` the `agentit` namespace" (i.e. any authenticated user with *any* role binding here) — tighten `chart/templates/deployment.yaml`'s `--openshift-sar` argument if a narrower audience is needed.

**Important**: this protects the browser-facing Route only. Argo Events Sensors call `/api/webhook/*` directly against the in-cluster Service (`http://agentit.agentit.svc:8080/...`), never through the Route, so they never go through this proxy regardless of `auth.enabled` — see the internal webhook token below.

The app itself never verifies the identity oauth-proxy hands it (`X-Forwarded-User`) — it trusts the header as-is, since the proxy is a sidecar in the same pod (reached over loopback), not a separate network hop. `get_current_user()` (`src/agentit/portal/helpers.py`) reads it, falling back to `"portal-user"` when absent (i.e. `auth.enabled=false`, local dev, or tests).

### CSRF protection

Applies to every browser-originated `POST`/`PUT`/`PATCH`/`DELETE` route, always on (no flag) — implemented as the standard double-submit-cookie pattern since the app has no session store. A `csrf_token` cookie is set on every response; `base.html`'s `htmx:configRequest` handler echoes it back as an `X-CSRF-Token` header on every htmx-boosted request (the whole `<body>` is `hx-boost="true"`, so this covers every `<form method="post">` without editing each template). A `csrf_token` form-field fallback also works for any non-JS submission.

Exempt: `/api/webhook/*` (Part 3 below secures those separately; they're not browser submissions and don't carry the cookie) and `/healthz`/`/readyz`.

### Internal webhook token (`/api/webhook/*`)

`/api/webhook/{assess,onboard,auto-apply,finding,remediate}` are called only by Argo Events Sensors (`chart/templates/argo-events/sensor-*.yaml`), never by a browser — always created regardless of `auth.enabled`/`argoEvents.enabled`. A `agentit-internal-webhook-token` Secret (`chart/templates/internal-webhook-token-secret.yaml`, auto-generated with `lookup`+`randAlphaNum`, same idempotent pattern as `postgres-secret.yaml`) is:

- Mounted into the app as `AGENTIT_INTERNAL_WEBHOOK_TOKEN` (optional, like `GITHUB_WEBHOOK_SECRET`).
- Read by each Sensor's HTTP trigger via `secureHeaders` (resolved from the Secret at trigger-fire time, not Helm-render time, so it always matches whatever Helm most recently wrote) and sent as `X-Internal-Webhook-Token`.
- Verified by `verify_internal_token` (`src/agentit/portal/routes/webhooks.py`) as a FastAPI dependency on all 5 routes.

`/api/webhook/github-push` is unaffected — it keeps its own pre-existing HMAC-SHA256 signature check against `GITHUB_WEBHOOK_SECRET`.

Like `GITHUB_WEBHOOK_SECRET`, `verify_internal_token` fails open (skips the check) if `AGENTIT_INTERNAL_WEBHOOK_TOKEN` isn't set in the app's env — but since the Secret is *always* templated (not gated behind a flag), that path should only be exercised in local dev/tests that never configure it, not in a real cluster.

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
