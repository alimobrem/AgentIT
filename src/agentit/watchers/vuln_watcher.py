"""Vulnerability watcher agent — monitors fleet for CVEs and triggers remediation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from agentit.consumer import EventConsumer
from agentit.events import EventPublisher, TOPIC_ALERTS, TOPIC_EVENTS
from agentit.watchers import record_tick

logger = logging.getLogger(__name__)

# Touch /tmp/heartbeat at least this often while sleeping between ticks, so
# the liveness probe's staleness check (900s in chart/templates/agents/
# vuln-watcher.yaml) reflects "is the process alive", not "did a tick just
# finish". Without this, any tick that completes (success or failure) is
# followed by a sleep of up to `--interval` (21600s/6h default) with nothing
# refreshing the heartbeat, so kubelet SIGKILLs the container ~15-19 minutes
# into every single sleep, forever -- see the incident writeup for the full
# postgres-tick-timestamp evidence.
_HEARTBEAT_REFRESH_SECONDS = 300


class VulnWatcher:
    """Long-lived agent that listens for assessment events, checks the fleet
    for critical/high findings, and triggers remediation when auto-mode is on.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        store: object,
        consumer: EventConsumer,
        interval: int = 21600,
    ) -> None:
        self._publisher = publisher
        self._store = store
        self._consumer = consumer
        self._interval = interval

    def _handle_event(self, event: dict) -> None:
        action = event.get("action", "")
        target = event.get("targetApp", "")
        if action == "assessment-complete":
            logger.info("Assessment completed for %s, checking for CVEs", target)
            click.echo(f"[vuln-watch] Assessment completed for {target}, checking for CVEs...", err=True)
            self._publisher.publish(
                TOPIC_EVENTS,
                agent_id="vuln-watcher",
                action="cve-check-triggered",
                target_app=target,
                summary=f"CVE check triggered by assessment of {target}",
            )

    async def check_fleet(self) -> None:
        """Scan the fleet for apps with critical/high findings and alert or remediate.

        ``self._store`` is the async-compatible store handed in by
        ``cli.py``'s ``vuln_watch`` command (``AsyncSQLiteStore`` or
        ``store_pg.AssessmentStore`` -- never the raw sync store, see
        docs/postgres-migration-plan.md), so every store call here is
        `await`ed directly. ``AutoMode``/``RemediationLoop`` are also
        genuinely async, so they're constructed with that same store
        object -- no bridging facade needed.
        """
        fleet = await self._store.get_fleet_data()
        click.echo(f"[vuln-watch] Monitoring {len(fleet)} apps", err=True)

        from agentit.automode import AutoMode
        from agentit.remediation_loop import RemediationLoop

        auto = AutoMode(store=self._store, publisher=self._publisher, llm_client=None)
        loop = RemediationLoop(store=self._store, publisher=self._publisher)

        for app_data in fleet:
            if app_data.get("critical_count", 0) > 0:
                self._publisher.publish(
                    TOPIC_ALERTS,
                    agent_id="vuln-watcher",
                    action="critical-findings-detected",
                    target_app=app_data["repo_name"],
                    severity="warning",
                    summary=f"{app_data['critical_count']} critical/high findings in {app_data['repo_name']}",
                )
                if await auto.is_enabled():
                    click.echo(
                        f"[vuln-watch] Auto-mode: running remediation loop for {app_data['repo_name']}...",
                        err=True,
                    )
                    try:
                        result = await loop.trigger(
                            repo_url=app_data["repo_url"],
                            app_name=app_data["repo_name"],
                            criticality=app_data.get("criticality", "medium"),
                            reason=f"critical findings detected ({app_data['critical_count']})",
                        )
                        click.echo(
                            f"[vuln-watch] Loop result: {result['outcome']} at step {result.get('step', '?')}",
                            err=True,
                        )
                    except Exception as exc:
                        logger.exception("Remediation loop failed for %s", app_data["repo_name"])
                        click.echo(f"[vuln-watch] Remediation loop failed: {exc}", err=True)
                        if self._publisher:
                            self._publisher.publish(
                                "agentit-alerts", agent_id="vuln-watcher",
                                action="remediation-failed", target_app=app_data.get("repo_name", ""),
                                severity="critical",
                                summary=f"Remediation loop failed: {exc}",
                            )

    async def run(self) -> None:
        """Main loop: poll events, check fleet, sleep.

        ``check_fleet`` is now a genuine coroutine (``AutoMode``/
        ``RemediationLoop`` are natively async) -- it's `await`ed directly
        here instead of being dispatched via ``asyncio.to_thread``, since
        wrapping an async function in `to_thread` would just add a
        redundant thread hop for no benefit.
        """
        click.echo(f"Starting vulnerability watcher (interval={self._interval}s)...", err=True)
        while True:
            try:
                events = self._consumer.poll_once()
                for event in events:
                    self._handle_event(event)
                await self.check_fleet()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, "vuln-watcher", success=True)
            except KeyboardInterrupt:
                click.echo("Vulnerability watcher stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("vuln-watch tick failed")
                click.echo(f"[vuln-watch] Error: {exc}", err=True)
                await record_tick(self._store, "vuln-watcher", success=False, error=str(exc))

            try:
                await self._sleep_with_heartbeat(self._interval)
            except KeyboardInterrupt:
                click.echo("Vulnerability watcher stopped.", err=True)
                break

    async def _sleep_with_heartbeat(self, seconds: int) -> None:
        """Sleep for ``seconds``, touching ``/tmp/heartbeat`` at least every
        ``_HEARTBEAT_REFRESH_SECONDS`` instead of only once before/after the
        whole sleep. See ``_HEARTBEAT_REFRESH_SECONDS``'s comment for why
        this matters whenever ``self._interval`` exceeds the liveness
        probe's staleness window.
        """
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, _HEARTBEAT_REFRESH_SECONDS)
            await asyncio.sleep(chunk)
            Path("/tmp/heartbeat").touch()
            remaining -= chunk
