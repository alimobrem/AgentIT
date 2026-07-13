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

**Current state on the live dev cluster (`api.aws-jb-acsacm-1.dev05.red-chesterfield.com`, applied 2026-07-13):** the exact `Subscription` above was applied via `oc apply -f -`. `openshift-operators` already has a cluster-wide `OperatorGroup` (`global-operators`, `AllNamespaces` install mode, pre-existing — same one Strimzi/AMQ Streams uses), so no new `OperatorGroup` was needed, matching the Kafka pattern exactly. The package (`cloud-native-postgresql`, Certified Operators catalog) offers channels `candidate`, `fast`, `stable`, and per-minor `stable-v1.2x` channels; `stable` (documented above) currently resolves to `cloud-native-postgresql.v1.29.0`.

**Blocked, not installed** — the `Subscription` is applied but has not resolved to a CSV (`status.currentCSV`/`status.state` are both empty). `oc describe subscription cloud-native-postgresql -n openshift-operators` shows:

```
type: ResolutionFailed
reason: ConstraintsNotSatisfiable
message: constraints not satisfiable: no operators found in package kyverno in
  the catalog referenced by subscription kyverno, subscription kyverno exists
```

Root cause: a **pre-existing, unrelated** `kyverno` `Subscription` already in `openshift-operators` (created 2026-07-11, not part of this work) points at a package that no longer exists in the `community-operators` catalog. OLM resolves all `Subscriptions` sharing an `AllNamespaces` `OperatorGroup` together as one dependency set, so that one broken subscription blocks resolution for every subscription in the namespace — confirmed by checking the other pre-existing subscriptions there (`amq-streams`, `openshift-pipelines-operator`, `servicemeshoperator3`): all three showed the same `ConstraintsNotSatisfiable` condition, but each still had a cached `currentCSV`/`AtLatestKnown` state from before the `kyverno` subscription broke — new subscriptions (like this one, at the time) never got to compute a `currentCSV` at all while the namespace-wide resolution was broken. This was not a permissions or catalog-source-connectivity issue (`oc auth can-i create subscriptions.operators.coreos.com` and `... get csv` both returned `yes`; all `CatalogSource`s reported `healthy: true`/`READY`).

**Resolved 2026-07-13 (follow-up session):** before touching anything, verified the `kyverno` `Subscription` was pure dead weight — `status.currentCSV`/`status.installedCSV` were empty since its creation two days earlier (it had *never* resolved to any CSV, not even once), no InstallPlan anywhere on the cluster ever referenced it, and there was no live Kyverno install to protect: no `kyverno`-named pods/Deployments/namespaces, and the `kyverno.io` CRDs (`ClusterPolicy`, `Policy`) don't exist on this cluster at all — so nothing (including AgentIT's own compliance-agent-generated Kyverno `Policy` manifests, which are template artifacts, not applied resources) was actively depending on it. Confirmed it was safe to delete outright and ran:

```bash
oc delete subscriptions.operators.coreos.com kyverno -n openshift-operators
```

OLM's resolver re-ran within ~2 minutes (`catalog-operator` logs in `openshift-operator-lifecycle-manager` stopped referencing `kyverno` and picked up `cloud-native-postgresql` immediately after). Result: `cloud-native-postgresql` resolved to `cloud-native-postgresql.v1.29.0` (state `UpgradePending` → InstallPlan `install-m27gf`, approval `Manual` like every other subscription in this namespace), and `amq-streams`/`openshift-pipelines-operator`/`servicemeshoperator3` all lost their `ResolutionFailed` condition and returned to normal (`AtLatestKnown`). The InstallPlan was manually approved (`oc patch installplan install-m27gf -n openshift-operators --type merge -p '{"spec":{"approved":true}}'`, same manual-approval convention already in use for every other subscription here) to complete end-to-end verification, and the InstallPlan reached `Complete`.

**New, separate, unrelated blocker found during verification — not fixed, out of scope:** the CNPG CSV is stuck in `Installing` because its controller pod can't pull its image:

```
Failed to pull image "docker.enterprisedb.com/k8s/edb-postgres-for-cloudnativepg@sha256:...":
unable to retrieve auth token: invalid username/password: unauthorized
Unable to retrieve some image pull secrets (postgresql-operator-pull-secret)
```

The CSV's install strategy references an `imagePullSecrets: [{name: postgresql-operator-pull-secret}]` Secret that has never existed on this cluster (`oc get secret postgresql-operator-pull-secret -n openshift-operators` → `NotFound`). This is expected for EDB's certified "EDB Postgres for Kubernetes" build (this package, `cloud-native-postgresql` from `certified-operators`, is EnterpriseDB's certified operator) — it requires a real registry entitlement/pull secret for `docker.enterprisedb.com` that was simply never provisioned. **This has nothing to do with the kyverno fix**; it only became visible once resolution was unblocked. Fixing it requires whoever owns EDB/Red Hat Marketplace credentials for this cluster to create the `postgresql-operator-pull-secret` Secret in `openshift-operators` — not attempted here, since no real credentials were available and fabricating one would be worse than leaving it visibly broken. Re-check with:

```bash
oc get subscriptions.operators.coreos.com cloud-native-postgresql -n openshift-operators -o custom-columns=CURRENTCSV:.status.currentCSV,STATE:.status.state
oc get csv cloud-native-postgresql.v1.29.0 -n openshift-operators -o jsonpath='{.status.phase}{"\n"}{.status.message}{"\n"}'
oc get pods -n openshift-operators | grep postgresql-operator
```

No `Cluster` CR, namespace, or `OperatorGroup` was created as part of either change, and nothing in the AgentIT chart/Argo CD `Application` was touched. `postgres.enabled` still cannot be safely set to `true` until the pull-secret issue above is resolved and the operator pod is actually `Running`.

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

### Incident 2026-07-13: EventListener silently stopped creating PipelineRuns (~06:49–15:26 UTC)

**Symptom:** GitHub push deliveries to the `agentit-ci-webhook` Route kept returning `202`, and the
`agentit-ci-listener` EventListener kept logging `triggers.started`/`triggers.done`, but no
PipelineRun was ever created — the `resources/create.go: creating resource ... pipelineruns` line
present in every healthy delivery simply stopped appearing after 06:24:44Z.

**What it *wasn't* (ruled out with evidence, in order investigated):**
- **Stuck/leaked EventListener pod state.** Restarted the `el-agentit-ci-listener` pod outright;
  redelivering a real prior GitHub push (`gh api .../deliveries/{id}/attempts`) immediately
  afterward still failed identically. Not the cause.
- **Missing/misconfigured `TriggerBinding`.** `oc get triggerbinding -n agentit` is empty and no
  matching `ClusterTriggerBinding` exists either — but the EventListener's `bindings:` block uses
  the (deprecated-but-still-functional) inline `name`/`value` form, which needs no separate
  `TriggerBinding` object at all. `oc describe eventlistener`'s "Kind: TriggerBinding" in its
  Bindings section is just the struct's default display value, not evidence of a broken lookup.
  Confirmed by 26+ successful PipelineRuns created earlier the same morning under this exact same
  binding config. Not the cause.
- **Etcd/API-server instability.** There *is* a real, recurring etcd member flake on this cluster
  (`etcd-ip-10-0-22-6.ec2.internal` readiness/liveness probe failures — seen at 07:36:36Z as a
  logged `etcdserver: request timed out` on the EventListener's own event-recorder, and again
  independently at 13:36–13:37Z and briefly during this investigation around 15:37–15:39Z, causing
  several `oc`/API calls to time out). `etcdctl endpoint health --cluster` is healthy as of this
  writing. This is a genuine, separate, ongoing low-grade cluster health issue worth someone with
  broader cluster-admin authority looking into, but proven **not** the cause of this specific
  incident (the PipelineRun-creation failure did not resolve when etcd/apiserver was healthy, and
  is timing-independent of the etcd blips).

**Actual root cause:** The `github` `ClusterInterceptor` on the `github-push` trigger has required
a `secretRef` (`github-webhook-secret` / key `secret`) since commit `fe69a89` ("Harden CI/CD
pipeline: authenticate webhook..."), which explicitly calls out in its own commit message that this
requires *manually* creating that Secret on-cluster **and** configuring the same value on the
GitHub repo's webhook. That manual step was never completed: `oc get secret github-webhook-secret
-n agentit` returned `NotFound`, and `gh api repos/alimobrem/AgentIT/hooks/651471119` showed no
`secret` key in `config` at all (last touched 2026-07-11, never since). With `secretRef` configured
but no secret ever set on GitHub's side, GitHub never sends an `X-Hub-Signature-256` header, and the
`github` interceptor's own source rejects every such request with `no X-Hub-Signature-256 header
set` — confirmed as the literal, repeated error in
`tekton-triggers-core-interceptors` pod logs (`openshift-pipelines` namespace) for every delivery
from 06:49:46Z onward, with zero occurrences before that. (Exactly why it worked from generation-2
sync until 06:24:44Z and not after is not fully reconstructed — plausibly the secret/GitHub-side
config was briefly correct and then lost — but is moot given the fix below is idempotent and
verified working going forward.)

**Fix applied (2026-07-13, ~15:26Z):**
1. `oc create secret generic github-webhook-secret -n agentit --from-literal=secret=<random 64-hex>`
   (not in git, per the "never commit secrets" rule — this is the documented, on-cluster-only
   pattern from `fe69a89`'s own commit message).
2. `gh api --method PATCH repos/alimobrem/AgentIT/hooks/651471119` with `config.secret` set to the
   same value, preserving the existing `url`/`content_type`/`insecure_ssl`.
3. Verified via `gh api .../deliveries/{id}/attempts` (redelivering a real prior push) that the
   `github` interceptor now returns `Continue:true` and the EventListener creates a PipelineRun
   end-to-end. The redelivered event happened to carry `after: 79b000212625ba549f570539f3ddb311ba0d9031`
   (main's HEAD at the time), so this same redelivery both confirmed the fix *and* triggered the
   real `agentit-ci-l5w58` PipelineRun that shipped that commit — see the Argo CD `history` list for
   the resulting sync.

**Side effect worth knowing about:** the app's Rollout also reads `GITHUB_WEBHOOK_SECRET` from this
same `github-webhook-secret` Secret (for the *separate* portal-native `/api/webhook/github-push`
hook, id `651731410`). That hook was already failing independently (`last_response: 302`, an
unrelated redirect/protocol issue on that hook's `url`) before this Secret existed, so creating it
doesn't newly break anything — but if that 302 issue ever gets fixed, that hook will *also* need a
matching secret configured on GitHub's side (it currently has none either) or it will start hard-
failing HMAC checks instead of silently 302'ing.

**Found but explicitly not fixed (out of scope — requires a chart/template change, not a live
cluster fix):** `chart/templates/argo-events/sensor-onboard.yaml` and
`sensor-auto-apply.yaml` both set `retryStrategy.factor` as `{value: 2.0}` (a nested object), which
Argo Events fails to parse as a number (`strconv.ParseFloat: parsing "{\"value\":2}": invalid
syntax`) — confirmed live via `oc logs -l sensor-name=agentit-onboard`: `failed to trigger actions,
invalid backoff configuration, invalid factor`. This means the `agentit-onboard` and
`agentit-auto-apply` Sensors currently **cannot fire their HTTP trigger at all**, regardless of the
webhook-token fix above. `sensor-finding-remediate.yaml` has the correct flat `factor: 2.0` and was
used to verify the internal-webhook-token post-deploy check instead (see below). The fix is a
one-line chart change (`factor: 2.0` instead of `factor: {value: 2.0}`) but is left for a follow-up
PR since this session is deploy-and-verify only.

**Rollout auto-promotion finding:** the canary `steps` are `[setWeight:10, pause:30s, setWeight:50,
pause:30s, setWeight:100]` with no `AnalysisTemplate`/manual gate. The entire canary for the
`79b0002` deploy advanced from 10% to 100% in well under two minutes — faster than a human (or this
agent, mid-way through reading PipelineRun logs) can realistically watch-and-intervene on. Pods came
up clean (no restarts, `/readyz`/`/healthz` responding 200 within ~10s of container start, no store-
init or `AGENTIT_DB_BACKEND` tracebacks) so no intervention was needed this time, but there is
currently no way to pause this rollout for a real manual health check before it reaches 100% short
of pre-emptively `oc patch`-ing the Rollout to add a longer/indefinite pause — which weren't done
here since it wasn't necessary and the task scope excluded touching Rollout config. Worth a
follow-up: either a real `AnalysisTemplate` (the repo's `release/analysis-template` skill already
generates these) or a much longer first pause.
