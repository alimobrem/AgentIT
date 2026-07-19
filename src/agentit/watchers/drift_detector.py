"""Drift detector agent — checks Argo CD applications for out-of-sync state."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from agentit import kube
from agentit.events import EventPublisher
from agentit.watchers import record_tick

logger = logging.getLogger(__name__)

# Either threshold alone is enough to alert (OR, not AND) -- a huge commit
# burst that's only a few minutes old is just as actionable a signal as a
# single commit that's been stuck for hours. Tuned to the 2026-07-17
# incident's real timeline (notify-argocd stuck for hours, several commits
# queued up behind it) without being so tight that a normal few-minute CI
# run duration alone would false-alarm.
_GITOPS_LAG_COMMIT_THRESHOLD = 3
_GITOPS_LAG_HOURS_THRESHOLD = 1.0


class DriftDetector:
    """Long-lived agent that polls Argo CD applications and publishes drift
    events when apps are OutOfSync, then unconditionally re-syncs them.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        interval: int = 600,
        store: object | None = None,
    ) -> None:
        self._publisher = publisher
        self._interval = interval
        self._store = store

    async def detect_once(self) -> list[dict]:
        """Query Argo CD applications and return any that are OutOfSync.

        Returns a list of dicts with keys: app, sync_status, health_status.
        """
        # Independent of the Argo Applications list below (a different
        # resource, ApplicationSet) -- runs every tick regardless of
        # whether that list succeeds.
        await self._check_applicationset_drift()

        app_list = await asyncio.to_thread(self._fetch_argo_apps)
        if app_list is None:
            click.echo("[drift-detect] No Argo CD access -- skipping", err=True)
            return []

        items = app_list.get("items", [])
        drifted: list[dict] = []

        for app in items:
            name = app.get("metadata", {}).get("name", "unknown")
            status = app.get("status", {})
            sync_status = status.get("sync", {}).get("status", "Unknown")
            health = status.get("health", {}).get("status", "Unknown")
            revision = status.get("sync", {}).get("revision", "")

            # Closes the second half of the unified delivery flow's verify
            # loop for GitOps deliveries (docs/unified-apply-flow.md
            # section (C)): a delivery through `route_and_deliver()`'s
            # infra-repo-commit branch can't verify synchronously the way a
            # direct apply does -- the actual cluster change only happens
            # once a human merges and Argo's own reconcile loop picks it up,
            # on Argo's schedule, not AgentIT's. This existing 10-minute
            # poll is the concrete mechanism that closes that loop, reusing
            # a poll it already runs rather than adding a new one.
            if revision:
                await self._maybe_close_gitops_delivery(name, revision)

            # Self-check only, deliberately: for the `agentit` Application
            # we know the real GitOps branch (`main`, same one
            # chart/templates/tekton/trigger.yaml's webhook filters on) --
            # for arbitrary fleet apps we'd have to guess, which would
            # violate the "surface real data, never fabricate" rule this
            # repo follows everywhere else. See
            # docs/cicd-stall-hardening-2026-07-17.md.
            if name == "agentit" and revision:
                await self._check_gitops_lag(app, revision)

            if sync_status == "OutOfSync":
                self._publisher.publish(
                    "agentit-events",
                    agent_id="drift-detector",
                    action="drift-detected",
                    target_app=name,
                    severity="warning",
                    summary=f"Argo CD app '{name}' is OutOfSync (health: {health})",
                )
                click.echo(f"[drift-detect] DRIFT: {name} is OutOfSync", err=True)
                drifted.append({"app": name, "sync_status": sync_status, "health_status": health})
                await self._maybe_auto_sync(name)

        click.echo(f"[drift-detect] Checked {len(items)} Argo CD apps", err=True)

        # API drift detection — check if cluster APIs changed since last run
        try:
            from agentit.platform_context import discover_platform
            from agentit.api_drift_detector import detect_drift

            ctx = await asyncio.to_thread(discover_platform)
            api_drift = detect_drift(ctx.available_kinds, ctx.installed_operators)
            if api_drift.has_breaking_changes:
                for removed in api_drift.removed_apis:
                    self._publisher.publish(
                        "agentit-events",
                        agent_id="drift-detector",
                        action="api-removed",
                        target_app="cluster",
                        severity="critical",
                        summary=f"API removed from cluster: {removed}",
                    )
                click.echo(f"[drift-detect] CRITICAL: {len(api_drift.removed_apis)} API(s) removed from cluster", err=True)

                # Auto-deprecate skills that generate removed API kinds
                try:
                    from agentit.skill_engine import load_skill
                    skills_dir = Path(__file__).parent.parent.parent.parent / 'skills'
                    if skills_dir.exists():
                        for md_file in skills_dir.rglob('*.md'):
                            skill = load_skill(md_file)
                            if skill and skill.status == 'active':
                                for output in skill.outputs:
                                    if output.lower() in [r.lower() for r in api_drift.removed_apis]:
                                        content = md_file.read_text(encoding='utf-8')
                                        if 'status: active' in content:
                                            updated = content.replace('status: active', 'status: deprecated', 1)
                                            updated = updated.replace('deprecated_reason: ""', f'deprecated_reason: "API {output} removed from cluster"', 1)
                                            md_file.write_text(updated, encoding='utf-8')
                                            self._publisher.publish('agentit-events', agent_id='drift-detector', action='skill-deprecated', target_app='cluster', severity='warning', summary=f'Auto-deprecated skill {skill.name}: {output} API removed')
                                            click.echo(f'[drift-detect] Auto-deprecated skill {skill.name}: {output} removed', err=True)
                except Exception as exc:
                    logger.debug('Skill deprecation failed: %s', exc)

            if ctx.deprecated_apis:
                click.echo(f"[drift-detect] WARNING: {len(ctx.deprecated_apis)} deprecated API(s) still in use", err=True)
            if api_drift.new_apis:
                click.echo(f"[drift-detect] INFO: {len(api_drift.new_apis)} new API(s) available", err=True)
        except Exception as exc:
            logger.debug("API drift check failed (non-fatal): %s", exc)

        return drifted

    def _fetch_argo_apps(self) -> dict | None:
        """List Argo CD Application resources via the kubernetes client."""
        try:
            # Namespaced list against openshift-gitops, not a cluster-scoped
            # list -- matches the namespace-scoped Role rbac.yaml grants
            # (`-argocd-read`, bound only in openshift-gitops) and the same
            # namespace= every other Argo Application lookup in this repo
            # already uses (health.py, fleet.py, delivery.py). Omitting it
            # here made this the one call site trying a cluster-scoped
            # list, which 403s even for an otherwise-correctly-privileged SA.
            items = kube.list_custom_resources(
                "argoproj.io", "v1alpha1", "applications", namespace="openshift-gitops",
            )
        except kube.KubeError as exc:
            logger.warning("Failed to fetch Argo apps: %s", exc)
            return None
        if not items:
            return None
        return {"items": items}

    async def _check_gitops_lag(self, argo_app: dict, deployed_revision: str) -> None:
        """Detect "commits merged to main aren't reaching the cluster" --
        the concrete signal that was missing during the 2026-07-17
        incident, where notify-argocd sat stuck (pod scheduling/etcd
        pressure) for hours with nothing telling anyone. Compares the
        `agentit` Application's actually-synced revision against real
        GitHub commit history for `main` (never a guess/fabrication --
        see ``github_pr.get_commits_behind``'s docstring). A GitHub call
        failure just skips this tick's check.
        """
        repo_url = argo_app.get("spec", {}).get("source", {}).get("repoURL", "")
        if not repo_url:
            return
        from agentit.portal.github_pr import get_commits_behind

        lag = await asyncio.to_thread(get_commits_behind, repo_url, deployed_revision, "main")
        if not lag or lag.get("ahead_by", 0) <= 0:
            return
        ahead_by = lag["ahead_by"]

        hours_behind = lag.get("hours_behind")
        over_commits = ahead_by > _GITOPS_LAG_COMMIT_THRESHOLD
        over_hours = hours_behind is not None and hours_behind > _GITOPS_LAG_HOURS_THRESHOLD
        if not (over_commits or over_hours):
            return

        hours_msg = f"{hours_behind:.1f}h" if hours_behind is not None else "an unknown time"
        summary = (
            f"agentit's deployed revision {deployed_revision[:12]} is {ahead_by} commit(s) "
            f"behind origin/main, oldest undeployed commit landed {hours_msg} ago -- the "
            f"GitOps pipeline may be stuck (check notify-argocd / Tekton pod scheduling)."
        )
        self._publisher.publish(
            "agentit-events",
            agent_id="drift-detector",
            action="gitops-lag-detected",
            target_app="agentit",
            severity="critical",
            summary=summary,
        )
        click.echo(f"[drift-detect] GITOPS LAG: {summary}", err=True)

    async def _check_applicationset_drift(self) -> None:
        """Self-heal the fleet-wide `agentit-managed-apps` ApplicationSet's
        git source repoURL if it's ever manually overwritten outside AgentIT.

        2026-07-18 incident (twice in one day): something entirely outside
        this repo's code ran `oc create`/`oc patch` directly against the
        live cluster and overwrote `spec.generators[0].git.repoURL` /
        `spec.template.spec.source.repoURL` with a bogus placeholder --
        confirmed via `metadata.managedFields` field-manager fingerprinting
        (`kubectl-create`/`kubectl-patch`, neither of which is this app).
        That broke GitOps rollout for the entire fleet both times until a
        human noticed and manually restored it.

        Only ever CORRECTS a drifted value back to the known-good one
        (`github_pr.expected_managed_apps_repo_url()`), applied via
        `github_pr.ensure_applicationset()` -- the exact same function
        onboarding already uses to write this spec, so there is one source
        of truth for "what correct looks like", not a second copy of the
        spec here. Never creates or deletes the object: that stays owned by
        onboarding's own `ensure_applicationset()` call
        (`routes/assessments.py`, `portal/delivery.py`) -- if the
        ApplicationSet doesn't exist yet, this is a no-op.
        """
        from agentit.portal import github_pr

        name = github_pr.MANAGED_APPS_APPLICATIONSET_NAME
        namespace = github_pr.MANAGED_APPS_APPLICATIONSET_NAMESPACE

        try:
            appset = await asyncio.to_thread(
                kube.get_custom_resource,
                "argoproj.io", "v1alpha1", "applicationsets", name, namespace=namespace,
            )
        except kube.KubeError as exc:
            logger.warning("Failed to fetch %s ApplicationSet: %s", name, exc)
            return

        if appset is None:
            # Not onboarded yet -- creation belongs to
            # ensure_applicationset() at onboarding time, never to routine
            # drift healing.
            return

        spec = appset.get("spec", {}) or {}
        generators = spec.get("generators") or [{}]
        generator_url = (generators[0] or {}).get("git", {}).get("repoURL", "")
        source_url = spec.get("template", {}).get("spec", {}).get("source", {}).get("repoURL", "")

        expected_url = github_pr.expected_managed_apps_repo_url()
        if generator_url == expected_url and source_url == expected_url:
            return

        drifted_url = generator_url or source_url
        click.echo(
            f"[drift-detect] DRIFT: {name} ApplicationSet repoURL is '{drifted_url}', "
            f"expected '{expected_url}' -- healing",
            err=True,
        )
        healed = await asyncio.to_thread(github_pr.ensure_applicationset, expected_url)
        action = "applicationset-repo-drift-healed" if healed else "applicationset-repo-drift-heal-failed"
        summary = (
            f"{name} ApplicationSet's git source repoURL had drifted to '{drifted_url}' "
            f"(expected '{expected_url}') -- "
            f"{'automatically restored' if healed else 'heal attempt FAILED, needs manual fix'}."
        )
        self._publisher.publish(
            "agentit-events",
            agent_id="drift-detector",
            action=action,
            target_app=name,
            severity="critical",
            summary=summary,
            details={
                "generator_repo_url": generator_url,
                "source_repo_url": source_url,
                "expected_repo_url": expected_url,
                "healed": healed,
            },
        )
        click.echo(f"[drift-detect] {summary}", err=True)

    async def _maybe_close_gitops_delivery(self, argo_app_name: str, synced_revision: str) -> None:
        """If ``synced_revision`` matches a pending GitOps delivery's
        committed SHA, kick off the shared verify-and-close tail
        (``portal/delivery.py::verify_and_close_delivery``) for it now,
        instead of on a fixed timer -- the concrete mechanism by which
        ``DriftDetector`` and the unified apply flow become one coherent
        system instead of two disconnected pieces.
        """
        if self._store is None:
            return
        try:
            pending = await self._store.list_pending_gitops_deliveries()
        except Exception as exc:
            logger.debug("Failed to list pending GitOps deliveries: %s", exc)
            return

        for delivery in pending:
            sanitized = delivery["app_name"].lower().replace("_", "-").replace(".", "-")
            if f"managed-{sanitized}" != argo_app_name:
                continue
            commit_sha = (delivery.get("details") or {}).get("outcomes", {}).get(
                "cluster_config", {},
            ).get("commit_sha", "")
            if not commit_sha or not synced_revision.startswith(commit_sha[:7]):
                continue

            from agentit.portal.delivery import verify_and_close_delivery

            namespace = sanitized
            click_msg = f"[drift-detect] GitOps delivery {delivery['id']} for {delivery['app_name']} synced (revision {synced_revision[:12]}) -- starting verify"
            logger.info(click_msg)
            try:
                await verify_and_close_delivery(
                    self._store, delivery["id"], delivery["assessment_id"],
                    delivery["app_name"], namespace, "infra-repo-commit",
                )
            except Exception as exc:
                logger.warning("Failed to verify/close GitOps delivery %s: %s", delivery["id"], exc)

    async def _maybe_auto_sync(self, app_name: str) -> None:
        """Patch the Application to trigger a sync, unconditionally.

        AutoMode (which used to gate this behind a settings toggle) has
        been removed: re-syncing a drifted Argo CD Application only ever
        re-applies what's already declared in Git and already merged by a
        human -- the exact same self-heal ``_check_applicationset_drift()``
        above already performs unconditionally for the fleet-wide
        ApplicationSet. There's no unreviewed mutation here to gate on a
        toggle; a human already approved this state the moment they merged
        the GitOps commit, so catching the cluster up to it is always safe.

        Logging still goes through ``self._store`` when one was handed in
        (``cli.py``'s ``drift_detect`` command); without one (e.g. a
        detector constructed without a store, as some tests do), the sync
        itself still runs, it just has nowhere to log the outcome.
        """
        click.echo(f"[drift-detect] Auto-syncing {app_name}...", err=True)
        try:
            await asyncio.to_thread(
                kube.custom_objects().patch_namespaced_custom_object,
                group="argoproj.io",
                version="v1alpha1",
                namespace="openshift-gitops",
                plural="applications",
                name=app_name,
                body={"operation": {"sync": {"revision": "HEAD"}}},
            )
            click.echo(f"[drift-detect] Sync triggered for {app_name}", err=True)
            try:
                await self._store.log_event(
                    "drift-detector", "drift-auto-synced", app_name, "info",
                    f"Auto-synced {app_name} after detected drift",
                )
            except Exception:
                logger.warning("Failed to log drift-auto-synced event", exc_info=True)
        except Exception as exc:
            logger.warning("Auto-sync failed for %s: %s", app_name, exc)
            click.echo(f"[drift-detect] Sync failed: {str(exc)[:100]}", err=True)
            try:
                await self._store.log_event(
                    "drift-detector", "drift-auto-sync-failed", app_name, "warning",
                    f"Auto-sync failed for {app_name}: {str(exc)[:200]}",
                )
            except Exception:
                logger.warning("Failed to log drift-auto-sync-failed event", exc_info=True)

    async def run(self) -> None:
        """Main loop: detect drift, sleep.

        ``detect_once`` is now a genuine coroutine -- it `await`s its own
        blocking kube/platform-discovery calls narrowly (via
        ``asyncio.to_thread`` at each specific call site) rather than the
        whole tick being dispatched to a worker thread.
        """
        click.echo(f"Starting drift detector (interval={self._interval}s)...", err=True)
        while True:
            try:
                await self.detect_once()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, "drift-detector", success=True)
            except KeyboardInterrupt:
                click.echo("Drift detector stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("drift-detect tick failed")
                click.echo(f"[drift-detect] Error: {exc}", err=True)
                await record_tick(self._store, "drift-detector", success=False, error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except KeyboardInterrupt:
                click.echo("Drift detector stopped.", err=True)
                break
