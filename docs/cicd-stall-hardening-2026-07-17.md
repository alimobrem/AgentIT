# CI/CD stall hardening — 2026-07-17 incident follow-up

**Status: implemented and shipped (this pass) for everything safely doable
from the app repo; one item below is a documented cluster-admin
recommendation, not applied live.**

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

### Cluster-level pruner (recommendation, not applied)

`oc get tektonconfig config -o yaml` shows:

```yaml
spec:
  pruner:
    disabled: false
    keep: 100
    resources:
      - pipelinerun
    schedule: 0 8 * * *
```

Two real gaps here, independent of the app-repo bug above:

- `schedule: 0 8 * * *` is **once a day**. Between runs, PipelineRuns (and
  their owned TaskRuns/pods) can accumulate for up to 24 hours before this
  cluster-level pruner ever looks at them — most of the "100+ un-GC'd"
  objects seen during the incident accumulated in that window. The
  namespace-level fix in this PR (10-minute CronJob, now actually working)
  covers this going forward for the `agentit` namespace specifically, but
  this cluster-wide default stays lax for every other namespace on the
  cluster too.
- `resources: [pipelinerun]` only. TaskRuns owned by a pruned PipelineRun
  do cascade-delete via Kubernetes owner-reference GC, so this is *usually*
  fine — but it means the pruner has no independent backstop for
  standalone TaskRuns (not part of a Pipeline) or for a PipelineRun whose
  owner-ref GC is delayed/stuck (e.g. during the exact kind of
  etcd/control-plane pressure this incident already involved).

**Recommended change** (cluster-admin action — this is a cluster-singleton
`TektonConfig`, not a namespaced object, so it affects every namespace on
the cluster, not just `agentit`; deliberately not applied live in this
pass):

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

- `keep: 50` (down from 100) — 50 completed PipelineRuns is generous
  headroom for any namespace's history/debugging needs while meaningfully
  bounding etcd object count.
- `resources: ["pipelinerun", "taskrun"]` — adds the independent TaskRun
  backstop described above.
- `schedule: "*/30 * * * *"` (every 30 minutes, down from daily) — keeps
  the cluster-wide safety net from ever letting a full day's backlog build
  up, without running so often it competes for API-server time with actual
  workloads.

This is standard, low-risk, additive Tekton-operator configuration (no
pods restart, no data loss — it only changes retention going forward), but
it's genuinely cluster-wide-scoped, so it should go through whatever
change process this cluster's admin(s) use rather than being applied
unilaterally from an app-repo PR.

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
| A: cluster `TektonConfig` pruner (`keep`, `resources`, `schedule`) | **Recommended only** — cluster-admin action, exact patch above |
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
- **The cluster-wide pruner gap remains open** until a cluster admin
  applies the recommended patch. The namespace-level CronJob fix (item A)
  substantially reduces the practical impact of that gap for the `agentit`
  namespace specifically (10-minute cadence vs. daily), so this is a
  defense-in-depth follow-up, not a blocking gap.
