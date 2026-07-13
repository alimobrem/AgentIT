"""Drift detector agent — checks Argo CD applications for out-of-sync state."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import click

from agentit import kube
from agentit.events import EventPublisher

logger = logging.getLogger(__name__)


class DriftDetector:
    """Long-lived agent that polls Argo CD applications and publishes drift
    events when apps are OutOfSync. Optionally auto-syncs when auto-mode is on.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        interval: int = 600,
    ) -> None:
        self._publisher = publisher
        self._interval = interval

    def detect_once(self) -> list[dict]:
        """Query Argo CD applications and return any that are OutOfSync.

        Returns a list of dicts with keys: app, sync_status, health_status.
        """
        app_list = self._fetch_argo_apps()
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
                self._maybe_auto_sync(name)

        click.echo(f"[drift-detect] Checked {len(items)} Argo CD apps", err=True)

        # API drift detection — check if cluster APIs changed since last run
        try:
            from agentit.platform_context import discover_platform
            from agentit.api_drift_detector import detect_drift

            ctx = discover_platform()
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

            if api_drift.has_warnings:
                click.echo(f"[drift-detect] WARNING: {len(api_drift.deprecated_apis)} deprecated API(s)", err=True)
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

    def _maybe_auto_sync(self, app_name: str) -> None:
        """If auto-mode is enabled, patch the Application to trigger a sync."""
        from agentit.automode import AutoMode
        from agentit.portal.store import AssessmentStore

        store = AssessmentStore()
        auto = AutoMode(store=store, publisher=self._publisher)
        if not auto.enabled:
            return

        click.echo(f"[drift-detect] Auto-syncing {app_name}...", err=True)
        try:
            kube.custom_objects().patch_namespaced_custom_object(
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

    def run(self) -> None:
        """Main loop: detect drift, sleep."""
        click.echo(f"Starting drift detector (interval={self._interval}s)...", err=True)
        while True:
            try:
                self.detect_once()
                Path("/tmp/heartbeat").touch()
            except KeyboardInterrupt:
                click.echo("Drift detector stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("drift-detect tick failed")
                click.echo(f"[drift-detect] Error: {exc}", err=True)

            try:
                time.sleep(self._interval)
            except KeyboardInterrupt:
                click.echo("Drift detector stopped.", err=True)
                break
