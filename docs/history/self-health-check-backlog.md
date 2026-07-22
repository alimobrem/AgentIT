# AgentIT self-health: what's checked, what self-heals, and what's next

**Status: 4 self-checks implemented and shipped (`watchers/self_health_check.py`); the rest of this doc is a prioritized backlog, not a promise.**

## Why this doc exists

A 2026-07-17/18 session surfaced a repeating pattern across eight distinct
incidents: a real AgentIT-infrastructure failure had **zero proactive
signal** — every one was found only because a human happened to notice
something looked off (PRs not landing, "I don't see the changes on the
cluster", a misleading badge, a screenshot) and asked. Each of those eight
bugs has since been fixed **point-by-point** (see the "Incident inventory"
table below for exactly which commit/doc fixed which). What was missing was
the *systematic* version of the fix: nothing periodically re-checked
AgentIT's own critical infrastructure the way `vuln-watcher`/`slo-tracker`
already do for onboarded apps, so the *next* similar-but-different bug would
still go undetected until a human noticed by chance.

This doc is the design record for closing that gap: what already existed
(so nothing here duplicates it), how each incident class was categorized,
what got built now, and — deliberately not everything — what's prioritized
backlog for later.

## 1. What visibility already existed (audited, not rebuilt)

| Surface | What it covers | Relevant to this work |
| --- | --- | --- |
| **Events feed** (`/events`, `/api/events`) | Every `store.log_event()` call across the codebase — the system-activity audit trail. Postgres-backed (`events` table), not Kafka-backed. | This is where every new self-check's result needs to land to actually be visible — see the Kafka-only gap below. |
| **Sitewide critical/high badge** (`base.html`'s `eventsDrawer()`) | Polls `/api/events`, badges the nav on any `severity in (critical, high)` event since last-seen. Already generic — reused as-is, not rebuilt. | New self-checks reuse this for free by publishing `critical`/`warning` severities through `store.log_event()`. |
| **Ledger / PR-attention redesign** (in progress, `feature/fleet-redesign`) | Per-app "needs you" attention surface over gates/deliveries/decisions. | Out of scope here — app-scoped, not infra-scoped; not touched by this work. |
| **Health page** (`/health`) | Live pod/pipeline/Argo/Kafka/circuit-breaker/credential status, all real-time queries on page load. | Extended with a new "AgentIT Self-Health" section (below) — reuses the page, not a new one. |
| **`DriftDetector`'s GitOps-lag check** (`_check_gitops_lag`, shipped 2026-07-18) | Compares the live `agentit` Argo Application's synced revision against `origin/main` via GitHub's Compare API; fires `critical`/`gitops-lag-detected` if >3 commits or >1h behind. | Covers incidents #1/#2 (see table) **after commits have already piled up**. The new CI-pipeline-stall check below is a strictly earlier, more direct signal for the same failure class — deliberately not a duplicate. |
| **`DriftDetector`'s ApplicationSet-drift self-heal** (`_check_applicationset_drift`, landed concurrently with this work) | Detects and corrects a drifted `agentit-managed-apps` ApplicationSet `repoURL` every tick. | Directly closes incident #6 with real self-healing — see that incident's row below. Not duplicated here. |
| **`github_pr.check_webhook_delivery_health()` + Health page "Webhook Deliveries" section** (landed concurrently with this work, same session) | Live, fleet-wide (every onboarded app), 60s-cached check of each app's most recent GitHub webhook delivery, rendered as its own Health page table. | This is the *primary* fix for incident #5's "nothing checks reachability" gap — landed independently, in parallel with this pass, targeting the exact same gap. **Reused, not duplicated**: this work's own webhook self-check calls this same function (see §3) instead of re-implementing GitHub delivery-history parsing a second way. The remaining gap this work's check closes: that Health-page section only *computes* on page load and never persists a result, so a failure is invisible on the sitewide badge unless a human happens to open `/health` — see §3. |

**A real gap found during this audit, not fixed here (flagged, not silently
left broken):** `DriftDetector._check_gitops_lag`, its newly-landed sibling
`_check_applicationset_drift` (the incident #6 self-heal), and
`VulnWatcher`'s detection events all call `EventPublisher.publish()` only —
**not** `store.log_event()`. Every event that actually reaches `/api/events`
(and therefore the sitewide badge) goes through the Postgres `events` table;
nothing in the running portal or these watchers bridges the
`agentit-events` Kafka topic into that table. `SloTracker.
_recommend_rollback()` already does this correctly (dual-write: publish
*and* `log_event`) — none of the three above do, which would mean a
`gitops-lag-detected` or an `applicationset-repo-drift-healed` event is
currently invisible on `/api/events` and the badge despite the README
describing both as already surfaced there. This watcher's own checks are
dual-written for exactly this reason (see `_publish_result`'s docstring).
**Recommended follow-up** (not done here, to avoid editing
`drift_detector.py` while it's under active concurrent development this
same session): audit every `EventPublisher.publish()` call site across
`watchers/*.py` for a missing companion `store.log_event()`, or add a small
dedicated bridge consumer.

## 2. Incident inventory: category and disposition

| # | Incident | Category | Disposition |
| --- | --- | --- | --- |
| 1 | CI pipeline silently hung for hours (git-identity bug → retry pileup) | **(b) detect & surface** — a stuck pipeline needs a human to look at node scheduling/pod state, not an automated restart | Root cause fixed (`baa9bd4`, `docs/cicd-stall-hardening-2026-07-17.md` items B/D). **New systemic check added**: `self-check-ci-pipeline` (this work) — catches a *future* stall of this shape directly, earlier than the lag check. |
| 2 | Deployment fell behind `main` for hours | **(b) detect & surface** | Fixed: `DriftDetector`'s `gitops-lag-detected` (already shipped, see table above). Not duplicated here. |
| 3 | "Deploy failed" badge conflated live-health with CI-build-status | **(c) new check class was the fix itself** — this *was* "does the deploy-status computation still distinguish live-health from CI-build-status", fixed at the source | Fixed (`bf74f1c`, `_get_deploy_status()`'s `unreleased_ci_failure` carve-out). Regression-tested in `tests/test_deploy_status.py`; **not** re-implemented as a periodic watcher check — this is a code-correctness property best guarded by unit tests against the real function, not a runtime signal. |
| 4 | Cleanup CronJob looked healthy but silently did nothing (word-splitting bug) | **(c) new check class needed** — "did this CronJob's Job exit 0" was already checkable; "did it actually accomplish anything" was not | The specific bug is fixed (`docs/cicd-stall-hardening-2026-07-17.md` §A). **New systemic checks added** (this work): `self-check-cronjobs` (generic Job-success-recency, covers *any* CronJob's Job failing) and `self-check-cleanup-effectiveness` (a stale-object backlog proxy — catches "exits 0 but did nothing" generically, not just this one historical bug). |
| 5 | GitHub webhook silently blocked (OAuth proxy + TLS) | **(c) new check class needed** — this is the canonical example: nothing checked "is my own webhook actually reachable end-to-end", only "is a hook registered" | Root cause fixed (`538a282`). **Primary fix landed concurrently, same session**: `github_pr.check_webhook_delivery_health()` + a fleet-wide, live "Webhook Deliveries" Health-page section (checks every onboarded app, not just AgentIT itself). **This work adds one thing on top, reusing that same function rather than re-implementing it**: `self-check-webhook` runs it periodically in the background and persists the result, so a regression (cert rotation, secret drift, a hook accidentally deleted) reaches the sitewide Events badge even if nobody has opened `/health` recently — the live check alone only updates when a human loads that page. |
| 6 | Cluster-wide ApplicationSet repo URL corrupted by an external `oc` command | **(a) actively self-healable** | Being addressed by a separate concurrent worker (`fix/gitops-applicationset-selfheal`) — deliberately not duplicated here; see that branch/PR for the self-heal mechanism. |
| 7 | Duplicate Fleet rows from a normalization gap | **(a) actively self-healable** | Fixed with real self-healing: DB-layer normalization trigger + `AssessmentStore.dedupe_repo_urls()`, running at startup and every 5 minutes (README's "Hardened against duplicate Fleet rows" section). Already exactly the pattern this doc's remaining backlog items should aim for. |
| 8 | Skill activation silently failed ("generated no output", LLM token truncation) | **(b) detect & surface** (the generation itself can't safely auto-retry with an unbounded budget — see backlog item below for a bounded, safe version) | Root cause fixed (`2bddea6`, manifest-sized token budget). See backlog item "Empty-generation detection" below for the systemic, not-yet-built version. |

## 3. What's built now (this work)

Four checks, in `watchers/self_health_check.py` (`SelfHealthCheck`, CLI
`agentit self-health-watch`, chart flag `agents.selfHealthCheck.enabled`,
watcher name `self-health-check`), picked as the highest-value/most-tractable
subset rather than an attempt to re-detect all eight historical bugs:

1. **`self-check-webhook`** — GitHub webhook delivery health (incident #5's
   exact gap: nothing checked reachability, only registration). **Reuses**
   `github_pr.check_webhook_delivery_health()` — the same live check landed
   concurrently, same session, backing the Health page's own "Webhook
   Deliveries" section — rather than a second, independent implementation.
   This check's own value-add is purely mechanical: running that same
   function periodically in the background and persisting the result
   (`store.log_event()`), so a failure reaches the sitewide badge even when
   nobody has opened `/health` to trigger the live check. See §1's table
   for the full before/after.
2. **`self-check-ci-pipeline`** — is the latest `agentit-ci` PipelineRun
   stuck Running past a generous threshold (incident #1's exact shape,
   earlier/more direct than the lag check).
3. **`self-check-cronjobs`** — has every non-suspended CronJob in the
   `agentit` namespace completed successfully on its most recent run
   (generalizes incident #4's "Job failure" half, for *any* current or
   future CronJob, not a hardcoded list).
4. **`self-check-cleanup-effectiveness`** — a stale-terminal-pod backlog
   proxy (generalizes incident #4's "exits 0 but did nothing" half).

All four are **detect-and-surface, not self-healing** — see the module
docstring for why (none of the underlying fixes are safe to apply
automatically without human judgment: re-registering an unknown webhook
state, restarting a wedged pipeline, or deleting cluster objects).

## 4. Prioritized backlog (not built, and why)

Ordered by estimated value; each item names the concrete signal it would
add and why it wasn't picked for this pass.

1. **Bridge the Kafka-only-publish gap** (see section 1's flagged finding
   above). *Why deferred*: touches `drift_detector.py`/`vuln_watcher.py`,
   both actively being edited or recently landed by concurrent work this
   session; higher risk of a real merge conflict than value gained in this
   pass. *Next step*: a small audit pass across every `EventPublisher.
   publish()` call site once the concurrent ApplicationSet work lands.
2. **CI trigger-never-fired detection.** Today's checks catch "a
   PipelineRun exists and is stuck" — nothing catches "a commit landed on
   `main` and no PipelineRun was ever created for it at all" (e.g. the
   Tekton `EventListener`/`Trigger` itself is down). *Why deferred*: needs
   a reliable way to enumerate "commits since the last PipelineRun's
   revision" without re-deriving `DriftDetector`'s existing GitHub-compare
   logic in a second place — worth doing as a follow-up to item 1, not
   independently.
3. **Empty/near-empty LLM-generation detection.** Incident #8's root cause
   (token truncation) is fixed, but nothing generically flags "a skill/
   proposal generation produced suspiciously little output" as a class —
   only this one specific cause of it. A bounded, safe version: check
   `skill-learner`/`capability-scout`'s own run history
   (`learning-run`/`capability-run` events) for an outcome that saved zero
   bytes without an explicit `error`, and surface (never auto-retry with a
   larger budget unbounded, since that risks runaway token spend).
4. **Deploy-status self-consistency as a periodic check.** Incident #3 is
   a code-correctness property, well-covered by `tests/test_deploy_status.py`
   today. A *runtime* self-check would only add value if it could observe
   the badge disagreeing with ground truth in production, which is exactly
   what a unit test already does more cheaply and more precisely — not
   re-proposed as a watcher check unless a *new*, different regression of
   this class is found in production.
5. **ApplicationSet drift detection as a standing check** (beyond the
   in-progress self-heal work for incident #6). Once that work lands,
   worth adding a periodic *read-only* verification that the
   ApplicationSet's `repoURL` still matches its expected value, independent
   of whatever triggers the self-heal — belt-and-suspenders detection in
   case the self-heal's own trigger condition has a gap.
6. **Registry/ImageStream health.** `docs/cicd-stall-hardening-2026-07-17.md`
   §A notes `oc adm prune images`' blob-storage GC is structurally
   cluster-admin-only. Worth a lightweight check that the `agentit`
   ImageStream's own tag count (not blob storage) stays bounded, as a
   companion to `self-check-cleanup-effectiveness` above — deferred since
   the CronJob's own tag-retention loop (already shipped) is the primary
   defense and no incident has yet shown it failing silently.

## 5. How a user sees and acts on a failure today

1. **Ambient**: the sitewide critical/high badge (top nav, every page)
   lights up within one tick (default 15 minutes) of any check going
   `warning`/`critical` — no navigation needed to notice something's wrong.
2. **Detail**: `/health`'s new "AgentIT Self-Health" section shows all four
   checks' current pass/fail state, in plain language, with a "Next step"
   line naming the concrete command/place to look — never a raw error dump.
3. **History**: every check result (pass *and* fail) is a real, queryable
   event on `/events`, filterable by agent (`self-health-check`) — so "was
   this actually healthy an hour ago, or has it been failing all day" is
   answerable without guessing.
4. **Escalation**: none of these four checks auto-remediate. A human reads
   the guidance line and acts (or decides it's a false positive and the
   threshold needs tuning — the constants at the top of
   `self_health_check.py` are deliberately named and commented with the
   reasoning behind each one, for exactly that future adjustment).
