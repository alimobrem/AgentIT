"""Drift detector agent — checks Argo CD applications for out-of-sync state."""

from __future__ import annotations

import json
import logging
import subprocess
import time

import click

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
        return drifted

    def _fetch_argo_apps(self) -> dict | None:
        """Try ``oc`` then ``kubectl`` to list Argo CD Application resources."""
        for cli in ("oc", "kubectl"):
            try:
                result = subprocess.run(
                    [cli, "get", "applications.argoproj.io", "-A", "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    return json.loads(result.stdout)
            except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
                logger.warning("%s query failed: %s", cli, exc)
        return None

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
            sync_result = subprocess.run(
                [
                    "oc", "-n", "openshift-gitops", "patch",
                    "applications.argoproj.io", app_name,
                    "--type=merge",
                    "-p", '{"operation":{"sync":{"revision":"HEAD"}}}',
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if sync_result.returncode == 0:
                click.echo(f"[drift-detect] Sync triggered for {app_name}", err=True)
            else:
                click.echo(f"[drift-detect] Sync failed: {sync_result.stderr[:100]}", err=True)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning("Auto-sync failed for %s: %s", app_name, exc)

    def run(self) -> None:
        """Main loop: detect drift, sleep."""
        click.echo(f"Starting drift detector (interval={self._interval}s)...", err=True)
        while True:
            try:
                self.detect_once()
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
