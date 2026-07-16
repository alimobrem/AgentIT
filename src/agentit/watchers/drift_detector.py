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


class DriftDetector:
    """Long-lived agent that polls Argo CD applications and publishes drift
    events when apps are OutOfSync. Optionally auto-syncs when auto-mode is on.
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
            items = kube.list_custom_resources("argoproj.io", "v1alpha1", "applications")
        except kube.KubeError as exc:
            logger.warning("Failed to fetch Argo apps: %s", exc)
            return None
        if not items:
            return None
        return {"items": items}

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
        """If auto-mode is enabled, patch the Application to trigger a sync.

        ``self._store`` is the ``AssessmentStore`` handed in by ``cli.py``'s
        ``drift_detect`` command. Without one (e.g. a detector constructed
        without a store, as some tests do), there's no settings table to
        check ``auto_mode`` against, so auto-sync is skipped entirely rather
        than guessing/constructing a throwaway store.
        """
        if self._store is None:
            return

        from agentit.automode import AutoMode

        auto = AutoMode(store=self._store, publisher=self._publisher)
        if not await auto.is_enabled():
            return

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
        except Exception as exc:
            logger.warning("Auto-sync failed for %s: %s", app_name, exc)
            click.echo(f"[drift-detect] Sync failed: {str(exc)[:100]}", err=True)

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
