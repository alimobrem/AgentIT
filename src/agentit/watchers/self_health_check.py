"""Self-health-check agent — periodically verifies AgentIT's own critical
infrastructure is actually *working end to end*, not just "is the pod
running". Applies this repo's own "assess for real problems, never fabricate
data" philosophy to AgentIT itself, systematically, instead of only when a
human happens to notice something is wrong and asks.

**Why this watcher exists.** A 2026-07-17/18 session surfaced a repeating
pattern: a real AgentIT-infrastructure failure (a stuck CI pipeline, a
deployment silently falling behind `main`, a webhook silently blocked for
hours, a cleanup CronJob "running" every 10 minutes while doing nothing) had
*zero* proactive signal — every one was found only because a human happened
to notice something looked off and asked. Some of those exact bugs are
already fixed point-by-point (see `docs/cicd-stall-hardening-2026-07-17.md`
and this repo's git history), but nothing generalized the *lesson*: AgentIT
had no systematic, periodic check of its own critical infrastructure the way
it systematically checks onboarded apps'. This watcher is that: four
self-checks, each a genuinely new kind of check (not just missing alerting
on an existing one), picked for being the most tractable, highest-value
subset of that incident list rather than an attempt to re-detect every
historical bug. See `docs/self-health-check-backlog.md` for the checks
deliberately deferred, and why.

**Webhook check reuses, rather than duplicates, a concurrently-landed
live check.** A separate pass landed `github_pr.check_webhook_delivery_health()`
plus a Health page "Webhook Deliveries" section (fleet-wide, live-checked
and cached on every page load) targeting the same 2026-07-18 incident. This
watcher's webhook check calls that same function instead of re-implementing
GitHub delivery-history parsing a second, subtly different way — its own
distinct value is running that check periodically *in the background*, so a
failing webhook reaches the sitewide Events badge even when nobody has
opened /health recently, not a second opinion on what "healthy" means.

**Each check publishes exactly one event per tick, success or failure** —
unlike `DriftDetector`'s `gitops-lag-detected` (Kafka-only, no
`store.log_event`), every event here is dual-written (`EventPublisher.
publish()` *and* `store.log_event()`, mirroring `SloTracker.
_recommend_rollback()`'s convention) so it's genuinely visible on
`/api/events`, the sitewide critical/high badge, and this watcher's own
section on the Health page — not just delivered to whatever else happens to
consume the `agentit-events` Kafka topic. Publishing on success too (not
only on failure) is deliberate: a Health-page "Self-Health" panel needs a
real "checked N minutes ago, currently healthy" state, not silence.

**Self-healing vs. surfaced-for-humans.** None of these four checks
auto-remediate. Each one detects and surfaces a condition a human should act
on (with concrete next-step guidance in `details.guidance`) — none of them
are safe to blindly auto-fix without oversight (e.g. this watcher will never
attempt to re-register a webhook against an unknown external state, restart
a pipeline, or delete cluster objects). This mirrors this repo's existing
split: `DriftDetector` *does* auto-sync Argo CD drift unconditionally,
because a sync only ever re-applies what's already declared in Git and
already merged by a human (safe/idempotent, no unreviewed mutation to gate);
nothing here is that safe.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from agentit import kube
from agentit.events import EventPublisher, TOPIC_EVENTS
from agentit.watchers import record_tick, sleep_with_heartbeat

logger = logging.getLogger(__name__)

AGENT_ID = "self-health-check"

# Self-check only, deliberately -- guessing another fleet app's webhook/CI
# setup would violate this repo's "never fabricate data" rule (same
# reasoning as DriftDetector's gitops-lag check, which is scoped to the
# `agentit` app for the exact same reason).
_SELF_APP_NAME = "agentit"
_WEBHOOK_PATH = "/api/webhook/github-push"

_WEBHOOK_CHECK_ACTION = "self-check-webhook"
_CI_STALL_CHECK_ACTION = "self-check-ci-pipeline"
_CRONJOB_CHECK_ACTION = "self-check-cronjobs"
_CLEANUP_CHECK_ACTION = "self-check-cleanup-effectiveness"

#: Every action this watcher can publish, in the stable display order the
#: Health page's "AgentIT Self-Health" panel renders them in.
CHECK_ACTIONS: tuple[str, ...] = (
    _WEBHOOK_CHECK_ACTION,
    _CI_STALL_CHECK_ACTION,
    _CRONJOB_CHECK_ACTION,
    _CLEANUP_CHECK_ACTION,
)

#: Plain-language display name per check, owned here (not re-derived by a
#: generic string-humanizer) since these are read by
#: ``helpers.get_self_health_check_states()`` for the Health page's
#: "AgentIT Self-Health" panel.
CHECK_LABELS: dict[str, str] = {
    # "(background check)" disambiguates this row from the Health page's
    # separate, live-on-page-load "Webhook Deliveries" section (which this
    # check's own computation reuses -- see check_once's docstring): that
    # section answers "right now"; this row answers "as of the last
    # background tick", and is what actually reaches the sitewide badge.
    _WEBHOOK_CHECK_ACTION: "GitHub webhook reachability (background check)",
    _CI_STALL_CHECK_ACTION: "CI pipeline progress",
    _CRONJOB_CHECK_ACTION: "Maintenance CronJob success",
    _CLEANUP_CHECK_ACTION: "Cleanup effectiveness",
}

# Sum of every agentit-ci task's own timeout (chart/templates/tekton/
# pipeline.yaml: git-clone 5m + run-tests 10m + build-image 15m +
# smoke-test-image 5m + notify-argocd 5m + a few 2m housekeeping tasks) is
# ~46 minutes in the worst case where every task takes its full timeout
# before failing/retrying once. 60 minutes gives real headroom above that
# sum without being so loose it would miss the *hours*-long stall the
# 2026-07-17 incident actually had.
_DEFAULT_CI_STALL_MINUTES = 60.0

# Generous vs. the longest `activeDeadlineSeconds` any CronJob's Job
# currently sets (300s/5m, tekton-cleanup) plus real-world scheduling
# delay -- large enough to never false-positive on a job that's still
# legitimately finishing, small enough to catch a CronJob whose Job has
# actually been failing for a while.
_DEFAULT_CRONJOB_GRACE_MINUTES = 20.0

# tekton-cleanup runs every 10 minutes and only needs to keep stale
# terminal pods *bounded*, not at zero -- a handful lingering between runs
# is normal. This threshold is about catching "cleanup stopped working
# again", not enforcing an exact retention count (the CronJob's own
# retention logic already owns that).
_DEFAULT_STALE_POD_AGE_HOURS = 2.0
_DEFAULT_STALE_POD_COUNT_THRESHOLD = 10


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse, tolerating a bare 'Z' suffix. ``None``
    on anything unparseable -- callers must treat that as "unknown", never
    "now" or "epoch"."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SelfHealthCheck:
    """Long-lived agent that periodically runs AgentIT's own functional
    self-checks (webhook reachability, CI pipeline progress, maintenance
    CronJob success, cleanup effectiveness) and publishes one event per
    check per tick, success or failure.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        store: object | None = None,
        interval: int = 900,
        namespace: str = "agentit",
        ci_stall_minutes: float = _DEFAULT_CI_STALL_MINUTES,
        cronjob_grace_minutes: float = _DEFAULT_CRONJOB_GRACE_MINUTES,
        stale_pod_age_hours: float = _DEFAULT_STALE_POD_AGE_HOURS,
        stale_pod_count_threshold: int = _DEFAULT_STALE_POD_COUNT_THRESHOLD,
    ) -> None:
        self._publisher = publisher
        self._store = store
        self._interval = interval
        self._namespace = namespace
        self._ci_stall_minutes = ci_stall_minutes
        self._cronjob_grace_minutes = cronjob_grace_minutes
        self._stale_pod_age_hours = stale_pod_age_hours
        self._stale_pod_count_threshold = stale_pod_count_threshold

    @staticmethod
    def _check_result(
        *, ok: bool | None, action: str, severity: str, summary: str,
        guidance: str | None = None, details: dict | None = None,
    ) -> dict:
        """``ok=None`` means "inconclusive this tick" (e.g. the check
        itself couldn't reach an API) -- distinct from ``True``/``False``
        so callers never mistake "couldn't check" for "checked and
        healthy"."""
        return {
            "ok": ok, "action": action, "severity": severity, "summary": summary,
            "guidance": guidance, "details": details or {},
        }

    def _get_agentit_repo_url(self) -> str | None:
        """AgentIT's own repo URL, read from the live `agentit` Argo CD
        Application (the same source of truth `DriftDetector`'s
        `_check_gitops_lag` uses) -- never a guess, never a config
        constant that could drift from what's actually deployed."""
        try:
            items = kube.list_custom_resources(
                "argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops",
            )
        except kube.KubeError:
            logger.warning("Failed to fetch the agentit Argo CD Application", exc_info=True)
            return None
        app = next((a for a in items if a.get("metadata", {}).get("name") == _SELF_APP_NAME), None)
        if app is None:
            return None
        return app.get("spec", {}).get("source", {}).get("repoURL") or None

    async def _check_webhook_reachability(self) -> dict:
        """Is AgentIT's own GitHub push webhook actually reaching this app
        end to end -- not just "is a hook registered" (`ensure_webhook`'s
        own idempotency check already answers that), but "have recent
        delivery *attempts* actually succeeded". This is the exact
        question nothing checked before the 2026-07-18 incident, where an
        oauth-proxy redirect plus a hardcoded `insecure_ssl` value left
        100% of deliveries failing for hours, discovered only when a human
        happened to ask about an unrelated badge.

        Reuses ``github_pr.check_webhook_delivery_health()`` -- the same
        live check that now backs the Health page's own "Webhook
        Deliveries" section -- as the single source of truth for
        *computing* this signal, rather than a second, independent set of
        GitHub API calls with its own classification. What this watcher
        adds on top: a *periodic, background* run of that same check
        (so a failing webhook reaches the sitewide badge and Events
        history even if nobody has opened /health recently), not a
        different way of deciding "healthy".
        """
        repo_url = await asyncio.to_thread(self._get_agentit_repo_url)
        if not repo_url:
            return self._check_result(
                ok=None, action=_WEBHOOK_CHECK_ACTION, severity="warning",
                summary=(
                    "Could not determine AgentIT's own repo URL from the live Argo CD "
                    "Application this tick -- skipping the webhook check."
                ),
            )

        from agentit.portal import github_pr

        health = await asyncio.to_thread(
            github_pr.check_webhook_delivery_health, repo_url, _WEBHOOK_PATH,
        )
        ok = health.get("ok")
        detail = health.get("detail", "")

        if ok is None:
            return self._check_result(
                ok=None, action=_WEBHOOK_CHECK_ACTION, severity="warning", summary=detail, details=health,
            )
        if ok is False:
            return self._check_result(
                ok=False, action=_WEBHOOK_CHECK_ACTION, severity="critical", summary=detail,
                guidance=(
                    "See this Health page's own 'Webhook Deliveries' section for live "
                    "per-app detail. For redirects (3xx): oauth-proxy --skip-auth-regex "
                    "must cover ^/api/webhook/ (chart/templates/deployment.yaml). For "
                    "TLS (status_code 0): hook insecure_ssl/secret vs ingress cert "
                    "(docs/deployment.md). For 502/503/504 with no recent successes: "
                    "confirm portal pods Ready after canary/rollout, then redeliver "
                    "or ping the hook."
                ),
                details=health,
            )
        return self._check_result(
            ok=True, action=_WEBHOOK_CHECK_ACTION, severity="info", summary=detail, details=health,
        )

    async def _check_ci_pipeline_progress(self) -> dict:
        """Is the latest `agentit-ci` PipelineRun actually making progress,
        or has it been stuck "Running" far longer than any real run
        (including retries) should ever take -- the direct, fast signal
        for the exact 2026-07-17 incident shape (`notify-argocd` stuck on
        pod scheduling/etcd pressure for hours), and a strictly earlier
        warning than `DriftDetector`'s `gitops-lag-detected` (which only
        fires once undeployed commits have piled up *and* aged past its
        own threshold).
        """
        try:
            runs = await asyncio.to_thread(
                kube.list_custom_resources, "tekton.dev", "v1", "pipelineruns",
                namespace=self._namespace,
            )
        except kube.KubeError as exc:
            return self._check_result(
                ok=None, action=_CI_STALL_CHECK_ACTION, severity="warning",
                summary=f"Could not query Tekton PipelineRuns to check CI pipeline health: {exc}",
            )

        ci_runs = [
            r for r in runs
            if r.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline") == "agentit-ci"
        ]
        if not ci_runs:
            return self._check_result(
                ok=True, action=_CI_STALL_CHECK_ACTION, severity="info",
                summary="No agentit-ci PipelineRun history to check yet.",
            )

        # list_custom_resources order is not guaranteed -- pick newest by time.
        ci_runs.sort(key=lambda r: r.get("metadata", {}).get("creationTimestamp") or "")
        latest = ci_runs[-1]
        name = latest.get("metadata", {}).get("name", "?")
        conditions = latest.get("status", {}).get("conditions", [{}])
        cond = conditions[0] if conditions else {}
        is_running = cond.get("status", "Unknown") == "Unknown"
        start_time = latest.get("status", {}).get("startTime", "")

        if not is_running:
            return self._check_result(
                ok=True, action=_CI_STALL_CHECK_ACTION, severity="info",
                summary=f"Latest agentit-ci PipelineRun ({name}) is not currently running.",
            )

        started = _parse_iso(start_time)
        if started is None:
            return self._check_result(
                ok=True, action=_CI_STALL_CHECK_ACTION, severity="info",
                summary=f"Latest agentit-ci PipelineRun ({name}) is running; start time unavailable to judge age.",
            )

        age_minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        if age_minutes > self._ci_stall_minutes:
            return self._check_result(
                ok=False, action=_CI_STALL_CHECK_ACTION, severity="critical",
                summary=(
                    f"agentit-ci PipelineRun {name} has been running for {age_minutes:.0f} "
                    f"minutes (> {self._ci_stall_minutes:.0f}m) -- the CI pipeline may be "
                    "stalled, the same shape as the 2026-07-17 incident."
                ),
                guidance=(
                    f"oc get pipelinerun {name} -n {self._namespace}; "
                    f"oc get pods -n {self._namespace} --field-selector=status.phase=Pending. "
                    "See docs/cicd-stall-hardening-2026-07-17.md."
                ),
                details={"pipelinerun": name, "age_minutes": round(age_minutes, 1)},
            )

        return self._check_result(
            ok=True, action=_CI_STALL_CHECK_ACTION, severity="info",
            summary=f"agentit-ci PipelineRun {name} is running normally ({age_minutes:.0f}m elapsed).",
        )

    async def _check_maintenance_cronjobs(self) -> dict:
        """Has every non-suspended CronJob in this namespace actually
        *completed successfully* on its most recent scheduled run --
        generalized across every CronJob the chart currently ships
        (tekton-cleanup, secret-rotation, the fleet-rescan jobs, ...)
        rather than a hardcoded list, so a future CronJob is covered for
        free. Catches "a maintenance job's Job has been failing" -- a
        different failure mode from, and complementary to, the
        effectiveness check below (which catches "the Job keeps
        succeeding but isn't accomplishing anything").
        """
        try:
            cronjobs = await asyncio.to_thread(kube.list_cronjobs, self._namespace)
        except kube.KubeError as exc:
            return self._check_result(
                ok=None, action=_CRONJOB_CHECK_ACTION, severity="warning",
                summary=f"Could not list CronJobs in {self._namespace} to check maintenance-job health: {exc}",
            )

        if not cronjobs:
            return self._check_result(
                ok=True, action=_CRONJOB_CHECK_ACTION, severity="info",
                summary=f"No CronJobs found in {self._namespace} to check.",
            )

        grace = timedelta(minutes=self._cronjob_grace_minutes)
        failing: list[str] = []
        for cj in cronjobs:
            if cj["suspended"]:
                continue
            last_schedule = _parse_iso(cj["last_schedule_time"])
            if last_schedule is None:
                continue  # never scheduled yet (e.g. just installed) -- nothing to judge
            last_success = _parse_iso(cj["last_successful_time"])
            if last_success is None or last_success < last_schedule - grace:
                failing.append(cj["name"])

        if failing:
            failing.sort()
            return self._check_result(
                ok=False, action=_CRONJOB_CHECK_ACTION, severity="warning",
                summary=(
                    f"{len(failing)} of {len(cronjobs)} CronJob(s) in {self._namespace} have "
                    f"not completed successfully on their most recent scheduled run: "
                    f"{', '.join(failing)}."
                ),
                guidance=(
                    f"oc get cronjob -n {self._namespace}; "
                    f"oc logs -n {self._namespace} job/<latest job for the failing CronJob>"
                ),
                details={"failing": failing, "total": len(cronjobs)},
            )

        return self._check_result(
            ok=True, action=_CRONJOB_CHECK_ACTION, severity="info",
            summary=(
                f"All {len(cronjobs)} CronJob(s) in {self._namespace} completed successfully "
                "on their most recent run."
            ),
        )

    async def _check_cleanup_effectiveness(self) -> dict:
        """Complements the CronJob-success check above: a CronJob whose
        Job exits 0 every time still isn't doing its job if the thing it's
        supposed to bound keeps growing anyway -- the exact "looks
        healthy, ran every 10 minutes, logged output, did nothing" shape
        of the 2026-07-18 tekton-cleanup word-splitting bug
        (docs/cicd-stall-hardening-2026-07-17.md section A), which a bare
        exit-code check could never have caught, and which was discovered
        only via deep manual investigation of an unrelated incident.
        Checked generically (a stale-object *count*, not "did this exact
        bug recur") so this also catches a different future regression of
        the same failure class.
        """
        try:
            stale_count = await asyncio.to_thread(
                kube.count_stale_terminal_pods, self._namespace, self._stale_pod_age_hours,
            )
        except kube.KubeError as exc:
            return self._check_result(
                ok=None, action=_CLEANUP_CHECK_ACTION, severity="warning",
                summary=f"Could not count stale terminal pods in {self._namespace}: {exc}",
            )

        if stale_count > self._stale_pod_count_threshold:
            return self._check_result(
                ok=False, action=_CLEANUP_CHECK_ACTION, severity="warning",
                summary=(
                    f"{stale_count} Failed/Succeeded pods older than "
                    f"{self._stale_pod_age_hours:g}h remain in {self._namespace} -- the "
                    "tekton-cleanup CronJob may be running without actually cleaning up "
                    "(a CronJob can report success while its cleanup logic silently does "
                    "nothing; see docs/cicd-stall-hardening-2026-07-17.md)."
                ),
                guidance=(
                    f"oc get pods -n {self._namespace} --field-selector=status.phase=Failed; "
                    "check the tekton-cleanup CronJob's latest Job logs for 'Deleted 0' lines."
                ),
                details={
                    "stale_terminal_pods": stale_count,
                    "threshold": self._stale_pod_count_threshold,
                },
            )

        return self._check_result(
            ok=True, action=_CLEANUP_CHECK_ACTION, severity="info",
            summary=(
                f"Only {stale_count} stale Failed/Succeeded pod(s) older than "
                f"{self._stale_pod_age_hours:g}h in {self._namespace} -- cleanup looks effective."
            ),
        )

    async def _publish_result(self, result: dict) -> None:
        """Dual-writes every check result -- Kafka (`EventPublisher.
        publish()`) *and* the store (`log_event()`) -- mirroring
        `SloTracker._recommend_rollback()`'s convention, deliberately
        unlike `DriftDetector`'s Kafka-only `gitops-lag-detected`: this
        watcher's whole purpose is human-visible self-health, so every
        result must land somewhere `/api/events` (and this watcher's own
        Health-page panel) can actually read it back, not only wherever
        else happens to consume the `agentit-events` topic.
        """
        details = dict(result.get("details") or {})
        if result.get("guidance"):
            details["guidance"] = result["guidance"]

        self._publisher.publish(
            TOPIC_EVENTS,
            agent_id=AGENT_ID,
            action=result["action"],
            target_app=_SELF_APP_NAME,
            severity=result["severity"],
            summary=result["summary"],
            details=details,
        )

        if self._store is None:
            return
        try:
            await self._store.log_event(
                AGENT_ID, result["action"], _SELF_APP_NAME, result["severity"],
                result["summary"], details=details,
            )
        except Exception:
            logger.warning(
                "Failed to persist %s self-health-check event", result["action"], exc_info=True,
            )

    async def check_once(self) -> list[dict]:
        """Run every self-check once and publish each result -- always all
        four, even if an earlier one is inconclusive, since they're
        independent (a webhook problem must never mask a CI-stall check,
        or vice versa)."""
        checks = [
            await self._check_webhook_reachability(),
            await self._check_ci_pipeline_progress(),
            await self._check_maintenance_cronjobs(),
            await self._check_cleanup_effectiveness(),
        ]
        for result in checks:
            await self._publish_result(result)
            status = "OK" if result["ok"] else ("SKIPPED" if result["ok"] is None else "FAIL")
            click.echo(
                f"[self-health-check] {result['action']}: {status} -- {result['summary']}",
                err=True,
            )
        return checks

    async def run(self) -> None:
        """Main loop: run every self-check, sleep. Sleeps via
        ``watchers.sleep_with_heartbeat`` (same helper ``skill-learner``/
        ``vuln-watcher`` use) so the liveness probe's staleness check stays
        fresh regardless of ``--interval``.
        """
        click.echo(f"Starting self-health-check (interval={self._interval}s)...", err=True)
        while True:
            try:
                await self.check_once()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, AGENT_ID, success=True)
            except KeyboardInterrupt:
                click.echo("Self-health-check stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("self-health-check tick failed")
                click.echo(f"[self-health-check] Error: {exc}", err=True)
                await record_tick(self._store, AGENT_ID, success=False, error=str(exc))

            try:
                await sleep_with_heartbeat(self._interval)
            except KeyboardInterrupt:
                click.echo("Self-health-check stopped.", err=True)
                break
