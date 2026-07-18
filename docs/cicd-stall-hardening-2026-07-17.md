# CI/CD stall hardening — 2026-07-17 incident follow-up

**Status: implemented and shipped, fully self-contained.**

**Update (2026-07-18, "enable all watchers" product decision):** this same
day, `argocd/application.yaml` was changed to enable every remaining
opt-in watcher/CronJob the chart ships (`agents.reassessScheduler` plus
`cronJobs.{cveScan,complianceRescan,dependencyUpdate}` — `vuln-watcher`,
`slo-tracker`, `drift-detector`, `skill-learner`, `capability-scout`, and
`cronJobs.costReport` were already live). This raises the steady-state pod
count and periodic CronJob burst load in the `agentit` namespace on the
same topology this doc's **section B** describes (3 schedulable
workers/3 tainted masters, one zone-pinned EBS PV per Tekton PipelineRun).
Live signal captured *while making that change*, for the record: `oc adm
top nodes` showed one control-plane master (`ip-10-0-64-50`) at
**79-103% CPU / 84-93% memory** across repeated samples, and single-resource
`oc get -o yaml` calls against the live `agentit` Argo CD `Application`
intermittently hit `context deadline exceeded` / `Unauthorized` (auth
errors that clear on retry are a known symptom of API-server/etcd
pressure, not a real credential problem) before succeeding on a later
retry — the same class of symptom section B's "0/6 nodes available" and
"`etcdserver: request timed out`" findings describe, observed independent
of any change made in this update. This doc's own risk-of-recurrence
assessment did **not** model added watcher/CronJob background load as a
variable — it was written assuming the lower pod count that predated this
decision. None of the 6 watchers are CPU/memory-heavy in steady state
(each requests 50-100m CPU / 128-256Mi memory; see `chart/values.yaml`),
and the CronJobs are one-shot batch Jobs, not always-on pods, so the
*steady-state* delta is small — but 3 of the 4 fleet-rescan CronJobs now
fire full-fleet LLM re-assessment passes within the same Monday-morning
window (04:00/06:00/06:00, `cveScan` moved to Tuesday to at least de-collide
the exact 06:00 duplicate with `costReport` — see `chart/values.yaml`'s own
comment), each spinning up a fresh batch pod plus outbound LLM calls per
tracked app. On a cluster already showing intermittent control-plane
pressure, a burst of concurrent CronJob pods scheduling at the same moment
as CI PipelineRun activity is exactly the kind of added load this doc's
section B/D already treats as a live risk, not a hypothetical one. Watch
`oc get pods -n agentit` / `oc get cronjob -n agentit` and `oc adm top
nodes` after the next few scheduled fires, same as this update's own
post-deploy verification did.

**Update (2026-07-18, self-containment pass):** AgentIT is a product
deployed onto arbitrary customers' OpenShift clusters — we cannot assume
any given customer's cluster has a well-configured (or even present)
cluster-wide `TektonConfig` pruner, or a cluster-admin who's scheduled `oc
adm prune images`. This pass closed that assumption out: AgentIT's own
namespace-scoped `tekton-cleanup` CronJob (`chart/templates/tekton/
cleanup-cronjob.yaml`) is now fully self-sufficient for the `agentit`
namespace's own object hygiene — nothing it relies on for its own
correctness depends on any cluster-level configuration outside what
AgentIT itself ships and manages via its own chart. The cluster-wide
pruner recommendation this doc originally made is superseded — see "A.
Cluster-level pruner" below for the full before/after and the one piece
(registry blob storage GC) that's a genuine, structural exception.

## Incident recap

On the night of 2026-07-17, commits merged to `main` stopped reaching the
live cluster (`agentit` ArgoCD Application, `openshift-gitops`,
`https://api.aws-jb-acsacm-1.dev05.red-chesterfield.com:6443`) for hours,
silently. Prior work this session diagnosed (and partially fixed) three
root causes:

1. Only 3 of the cluster's 6 nodes are schedulable workers (the other 3 are
   tainted control-plane masters) — `notify-argocd`'s pod hit `0/6 nodes
   available: PV node-affinity mismatches + untolerated taints`, and
   separately `etcdserver: request timed out` during Task resolution.
   100+ un-GC'd PipelineRun/TaskRun objects and dozens of stale terminal
   pods (37 were manually deleted, one-time) were suspected of adding to
   control-plane/etcd pressure.
2. `_get_deploy_status()` in `src/agentit/portal/routes/health.py` used to
   report "Deploy failed" from the latest CI run's outcome even when that
   run never produced/deployed an image — fixed already, commit `bf74f1c`.
   **Not revisited here.**
3. `notify-argocd`'s `runAfter: [run-tests, smoke-test-image]` meant a
   hanging/failing `run-tests` (an unrelated git-identity test bug) could
   block `notify-argocd` from ever running — fixed already, commit
   `baa9bd4`. **Not revisited here.**

Everything above was worked around manually (pod cleanup, pipeline retry),
not durably fixed. This pass investigated and hardened items A/B/D from
that list; item C (alerting) was net-new. Findings and fixes below are
based on live evidence gathered against the real cluster on 2026-07-18,
not assumptions.

## A. Tekton pod/object garbage collection

### The real bug (fixed)

`chart/templates/tekton/cleanup-cronjob.yaml` already existed — a CronJob
running every 10 minutes that lists and deletes stale pods, old
PipelineRuns, orphaned affinity-assistant pods/StatefulSets/PVCs, and
orphaned agent Jobs. It *looked* like a working mitigation. It was not:

Every multi-field loop used this pattern:

```sh
for pod in $(oc get pods ... -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.startTime}{"\n"}{end}'); do
  NAME=$(echo "$pod" | awk '{print $1}')
  STARTED=$(echo "$pod" | awk '{print $2}')
  if [ -z "$NAME" ] || [ -z "$STARTED" ]; then continue; fi
  ...
done
```

`for x in $(cmd)` word-splits the command substitution's output on **all**
whitespace — spaces *and* newlines — not per-line. So each loop iteration
only ever received a single token (one pod name, or one timestamp, on
alternating iterations), never a `"name timestamp"` pair. `awk '{print
$2}'` on a single-token line is always empty, so `STARTED` (or `PR_NAME`,
`CREATED`, etc. in the other four affected loops) was **always empty**,
the `[ -z ... ] && continue` guard fired on **every** iteration, and none
of these five loops ever deleted anything — confirmed live: the CronJob's
own logs showed `Deleted 0 old PipelineRuns` and pods that failed at
`2026-07-18T03:19:37Z` were still present, un-cleaned, at
`2026-07-18T15:50Z` (12+ hours later), despite the CronJob visibly
"running" every 10 minutes the whole time.

Only two of the seven loops were unaffected: the succeeded-pods loop
(`oc get pods ... -o name`, one field per item, no pairing needed) and the
orphaned-PVCs loop (same reason) — which is exactly why the CronJob's logs
showed real deletions for succeeded pods but zero for everything else.

**Fix**: redirect each multi-field `oc get ... -o jsonpath=...` query into
a file and consume it with `while read -r FIELD1 FIELD2; do ... done <
file`, which preserves the per-line pairing. Applied to all five affected
loops (failed pods, orphaned affinity-assistant pods, orphaned
affinity-assistant StatefulSets, orphaned agent Jobs, old PipelineRuns).
Verified with a functional test (`tests/test_helm_templates.py::
TestTektonCleanup`) that executes the real rendered script against a fake
`oc` on `PATH` — confirmed to fail against the pre-fix script (7 of 10 new
assertions failed) and pass against the fix.

### Cluster-level pruner — superseded, no longer a meaningful gap (update 2026-07-18)

This subsection originally documented a cluster-admin recommendation as a
"defense-in-depth" backstop. It's been superseded: AgentIT cannot assume
any given customer's cluster has this configured, or even has the Tekton
operator's `TektonConfig` CRD installed at all — a real dependency on
something entirely outside AgentIT's own control. The two real gaps
originally found here are now both closed **inside AgentIT's own
namespace-scoped CronJob** instead:

`oc get tektonconfig config -o yaml` (on a cluster that has this CRD)
showed:

```yaml
spec:
  pruner:
    disabled: false
    keep: 100
    resources:
      - pipelinerun
    schedule: 0 8 * * *
```

- **Gap 1 — `schedule: 0 8 * * *` (once a day).** Already covered: the
  `tekton-cleanup` CronJob (this app repo) runs every 10 minutes in the
  `agentit` namespace, independent of whatever schedule (or absence
  thereof) any cluster-wide pruner uses.
- **Gap 2 — `resources: [pipelinerun]` only.** TaskRuns owned by a pruned
  PipelineRun cascade-delete via Kubernetes owner-reference GC, so a
  `pipelinerun`-only pruner is *usually* fine — but it has no independent
  backstop for standalone TaskRuns (never part of a Pipeline) or for a
  PipelineRun whose owner-ref GC is delayed/stuck (e.g. during the exact
  kind of etcd/control-plane pressure this incident already involved).
  **Closed in this pass**: `cleanup-cronjob.yaml` gained its own
  standalone/orphaned-TaskRun loop (`tests/test_helm_templates.py::
  TestTektonCleanup`, the `*taskrun*` tests) that deletes both cases
  directly, on the same 24-hour age basis as the PipelineRun loop, using
  its own cutoff computation — it doesn't wait on, or need, the
  PipelineRun loop above it to have run first, and it doesn't care whether
  any cluster-wide `TektonConfig` pruner exists, is configured, or is even
  installed.

Applying the previously-recommended cluster-admin patch below is now
**genuinely optional, redundant defense-in-depth at best** — not a gap
AgentIT depends on being closed:

```bash
oc patch tektonconfig config --type=merge -p '{
  "spec": {
    "pruner": {
      "keep": 50,
      "resources": ["pipelinerun", "taskrun"],
      "schedule": "*/30 * * * *"
    }
  }
}'
```

If a cluster admin wants cluster-wide coverage for namespaces *other than*
`agentit` too, this patch is still safe, low-risk, additive Tekton-operator
configuration (no pod restarts, no data loss). But `agentit`'s own object
hygiene no longer depends on it — or on the `TektonConfig` CRD existing on
the cluster at all.

### Image-tag accumulation — same gap shape, found in this pass, closed the same way

Auditing for other places that implicitly assumed a cluster-provided
backstop surfaced one more: `build-image` (`chart/templates/tekton/
pipeline.yaml`) pushes a new, uniquely-tagged image (the git revision) to
the `agentit` ImageStream on every CI run — the exact same unbounded
per-run accumulation shape as PipelineRuns/TaskRuns, normally bounded on a
real OpenShift cluster by a cluster-admin-run `oc adm prune images`. That
command is no more guaranteed to be scheduled on an arbitrary customer
cluster than the `TektonConfig` pruner is — same class of assumption,
same fix approach: `cleanup-cronjob.yaml` gained a namespace-scoped
image-tag-retention loop that keeps the 10 most recent tags on the
`agentit` ImageStream and prunes the rest, needing only a namespaced RBAC
grant (`imagestreams`: get, `imagestreamtags`: delete) this chart already
ships for the `pipeline` ServiceAccount (`chart/templates/tekton/
rbac.yaml`).

**One piece of this genuinely can't be made fully self-contained, and
that's a structural fact, not a missed effort**: `oc adm prune images`
also reclaims the underlying shared *blob storage* in the registry, which
requires inspecting every namespace's image references to know which
blobs are safe to delete (layers are content-addressed and shared across
images/namespaces) — that needs the cluster-scoped `system:image-pruner`
role. AgentIT's own namespaced `pipeline` ServiceAccount will never hold
that on an arbitrary customer cluster, by design (granting it would mean
every AgentIT install needs a cluster-admin-level grant just to run CI,
which is a far bigger ask than anything else this chart requires). What
this pass's loop *does* fully own, self-contained: bounding the
`agentit` ImageStream's own tag/object count — the etcd-object-growth
analog to the PipelineRun/TaskRun problem, and the part that's actually
namespace-scoped. Underlying blob reclaim remains a cluster-admin action,
exactly like registry storage housekeeping on any Kubernetes distribution
that isn't OpenShift (which has no ImageStream concept at all — this loop
already tolerates that by treating a missing `imagestream` kind or object
as a no-op, same as every other loop in this CronJob tolerates a missing
resource).

## B. Node scheduling fragility for CI pods (fixed)

Live topology check (`oc get nodes`, `oc get nodes -o
jsonpath='...topology.kubernetes.io/zone'`, `oc get storageclass`):

- 6 nodes total: 3 workers (schedulable), 3 control-plane masters (tainted
  `node-role.kubernetes.io/master:NoSchedule`).
- Exactly **one node per AWS zone pair**: `us-east-1a` has 1 worker + 1
  master, `us-east-1b` has 1 worker + 1 master, `us-east-1c` has 1 worker +
  1 master.
- The chart's only storage classes are `gp2-csi`/`gp3-csi`
  (`ebs.csi.aws.com`, `volumeBindingMode: WaitForFirstConsumer`) — AWS EBS
  volumes are zone-scoped once bound.

`chart/templates/tekton/trigger.yaml` provisions the pipeline's `source`
workspace as a `volumeClaimTemplate` (`ReadWriteOnce`, `2Gi`, per
PipelineRun). Once the first task in a run (`git-clone`) schedules and
binds that PVC, every later task sharing the same workspace — including
`notify-argocd` — is pinned to whichever single AWS zone that PVC bound
in. With exactly one schedulable worker per zone, that means each
PipelineRun effectively has **one** viable node for the whole run's
duration. If that one node is briefly saturated by concurrent CI activity
(exactly the kind of load this incident's un-GC'd backlog was creating),
every later task in that run — including the one that's supposed to
promote the build to the live Application — has **zero** schedulable
nodes: the observed `0/6 nodes available: PV node-affinity mismatches
(the other 2 zones' workers) + untolerated taints (all 3 masters)`.

**Fix**: added a `taskRunSpecs` `podTemplate.tolerations` entry for
`notify-argocd` (in `trigger.yaml`'s `TriggerTemplate`) tolerating the
control-plane taint, giving it a same-zone fallback node (the zone's
master) when the zone's one worker is unavailable. Scoped to
`notify-argocd` only, deliberately:

- It's a single `sed` + `oc apply` — 64Mi/50m request, no build/test
  compute load — so it adds negligible extra pressure to the
  already-etcd-sensitive control-plane nodes it might land on.
- `run-tests` (with a Postgres sidecar) and `build-image` (a real
  buildah build) were **not** given the same toleration — scheduling
  actual build/test compute onto control-plane nodes under load would
  make the etcd/control-plane pressure problem worse, not better, which
  is exactly the failure mode item D below is careful to avoid
  reproducing via retries.

This does not touch any node taint, label, or the cluster's node topology
itself — it only changes what the *pipeline* tolerates, per the task's
scope boundary.

Verified with `helm template --set tektonCI.enabled=true` (renders
correctly) and a new `tests/test_helm_templates.py::TestTektonTrigger`
class asserting the toleration is present and the existing `build-image`
`stepSpecs` override survives alongside it.

## C. Alerting for a stuck GitOps pipeline (new)

Checked first for anything equivalent already in place:
`_get_deploy_status()` (`routes/health.py`) already compares the
*running* instance's own commit against the *latest PipelineRun's target
revision* — but that only catches a canary rolled back **after** a
PipelineRun succeeded. It says nothing if new commits landed on `main` and
no PipelineRun (or a stuck one) ever promoted them — exactly what
happened on 2026-07-17. Nothing else in the codebase compared the
deployed revision against real upstream commit history.

**What's built**: `DriftDetector` (`src/agentit/watchers/drift_detector.py`)
already polls the `agentit` Argo Application every 10 minutes
(`detect_once`). Extended it to, for the `agentit` app specifically (a
deliberate self-check only — guessing another fleet app's default branch
would violate this repo's "never fabricate data" rule), call a new
`github_pr.get_commits_behind(repo_url, deployed_revision, "main")`
(GitHub Compare API, `GET /repos/{owner}/{repo}/compare/{base}...{head}`)
and, if the deployed revision is more than **3 commits** *or* **1 hour**
behind `main`'s real HEAD (either threshold alone is enough — a commit
burst is just as actionable as one commit stuck for a long time),
publishes a `severity="critical"`, `action="gitops-lag-detected"` event
via the existing `EventPublisher`.

That event needs no new UI: `base.html`'s existing `eventsDrawer()`
component already polls `/api/events` and badges the sitewide nav on any
`critical`/`high`-severity event — the exact "visible alert/banner in the
portal" the task asked for already exists in generic form; this change
just feeds it a real, previously-missing signal instead of building new
UI. `get_commits_behind` works without a `GITHUB_TOKEN` (unlike every
other function in `github_pr.py`) since GitHub's compare endpoint is
unauthenticated-readable for public repos, so a missing token can never
silently disable this specific check.

Verified with:
- `tests/test_deploy_status.py` (`get_commits_behind`: in-sync, lag-with-
  hours-computed, no-token-required, API-failure-returns-empty).
- `tests/test_drift_detector.py::TestGitopsLagDetection` (publishes on
  either threshold, stays silent when in sync or only trivially behind,
  never touches non-`agentit` apps, never crashes the tick on a GitHub API
  failure).

## D. `notify-argocd` timeout and retries

Timeout raised `2m0s` → `5m0s` (`chart/templates/tekton/pipeline.yaml`) —
the observed failure mode (pod stuck Pending on scheduling, or the
controller hitting `etcdserver: request timed out` resolving the Task)
can plausibly take longer than 2 minutes to clear on its own, especially
before the item B toleration fix existed live.

`retries` deliberately **left at 1** (not increased). Earlier the same
session, a *different* task's retry pileup under node-resource exhaustion
(fresh pod per retry, same resource-starved nodes) turned one hang into a
10-minute cascading failure — more retries compound the resource pressure
that caused the original delay, they don't fix it. The toleration fix
(item B) addresses the actual scheduling constraint directly; a longer
timeout gives real transient delays (etcd pressure, brief saturation)
room to clear; neither needs a second retry attempt layered on top, and a
second attempt would only add another fresh pod's worth of scheduling
pressure if the underlying cause hasn't cleared.

## Summary: what shipped vs. what's a recommendation

| Item | Disposition |
| --- | --- |
| A: cleanup CronJob word-splitting bug | **Fixed** (app repo, `chart/templates/tekton/cleanup-cronjob.yaml`) |
| A: standalone/orphaned TaskRun cleanup | **Fixed** (app repo, `cleanup-cronjob.yaml`, self-contained — closes the gap the cluster pruner recommendation below used to cover) |
| A: `agentit` ImageStream tag-count retention | **Fixed** (app repo, `cleanup-cronjob.yaml`, self-contained — keeps the 10 most recent tags) |
| A: cluster `TektonConfig` pruner (`keep`, `resources`, `schedule`) | **Superseded — now optional, redundant defense-in-depth** (AgentIT's own CronJob no longer depends on it; patch still documented above for admins who want cluster-wide coverage) |
| A: registry blob-storage GC (`oc adm prune images`) | **Structurally cluster-admin-only** — needs the cluster-scoped `system:image-pruner` role to inspect blob references across every namespace; AgentIT's namespaced ServiceAccount cannot do this on any customer cluster, by design. Not a gap in AgentIT's own namespace hygiene (tag-count retention above already bounds that) |
| B: `notify-argocd` PV-affinity/taint scheduling | **Fixed** (app repo, `chart/templates/tekton/trigger.yaml` toleration) |
| C: stuck-GitOps-pipeline alerting | **Fixed** (app repo, `DriftDetector` + `github_pr.get_commits_behind`, reuses existing events badge UI) |
| D: `notify-argocd` timeout/retries | **Fixed** (app repo, timeout only; retries deliberately unchanged) |

## Risk-of-recurrence assessment

- **The actual root cause most responsible for "silent for hours"** — the
  cleanup CronJob's dead pruning loops, which let 100+ objects and dozens
  of stale pods pile up unchecked despite visibly "running" — is fixed and
  regression-tested against a fake `oc`, not just reasoned about. This is
  the highest-confidence fix in this pass.
- **The scheduling fragility that took down `notify-argocd` specifically**
  is meaningfully improved (a second, same-zone fallback node) but not
  eliminated — if a whole zone's worker *and* master are both saturated or
  down, this pipeline is still topology-constrained to 3 real zones. That
  residual risk is inherent to running CI on a 3-worker/3-master cluster
  with zone-scoped EBS-backed workspaces and is explicitly out of this
  pass's scope (no node taint/label/topology changes).
- **Detection latency** for a stuck pipeline is now bounded (DriftDetector's
  existing 10-minute tick) instead of unbounded ("until a human notices").
  The 3-commits/1-hour thresholds are a reasonable first cut, not
  scientifically tuned — worth revisiting after this fires for real once.
- **The cluster-wide pruner gap is closed, not just reduced.** As of this
  pass, `agentit`'s own object hygiene (PipelineRuns, standalone/orphaned
  TaskRuns, and now ImageStream tag count) is fully self-contained inside
  AgentIT's own namespace-scoped CronJob — it does not depend on any
  cluster-admin action, any cluster-wide `TektonConfig` pruner existing or
  being configured a particular way, or that CRD even being installed.
  This matters specifically because AgentIT ships to arbitrary customers'
  OpenShift clusters, where none of that can be assumed. The one genuinely
  structural exception — registry blob-storage GC via `oc adm prune
  images` — needs cluster-scoped RBAC AgentIT's own ServiceAccount will
  never hold on a customer cluster; that's a real, explained limit, not an
  oversight, and it doesn't affect the etcd-object-count problem this
  incident was actually about (that's fully covered by the tag-count
  retention loop, which is namespace-scoped).
