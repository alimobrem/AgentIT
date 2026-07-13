"""Vulnerability watcher agent — monitors fleet for CVEs and triggers remediation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from agentit.consumer import EventConsumer
from agentit.events import EventPublisher, TOPIC_ALERTS, TOPIC_EVENTS
from agentit.portal.store import AssessmentStore
from agentit.watchers import record_tick

logger = logging.getLogger(__name__)


class VulnWatcher:
    """Long-lived agent that listens for assessment events, checks the fleet
    for critical/high findings, and triggers remediation when auto-mode is on.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        store: AssessmentStore,
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

    def check_fleet(self) -> None:
        """Scan the fleet for apps with critical/high findings and alert or remediate."""
        fleet = self._store.get_fleet_data()
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
                if auto.enabled:
                    click.echo(
                        f"[vuln-watch] Auto-mode: running remediation loop for {app_data['repo_name']}...",
                        err=True,
                    )
                    try:
                        result = loop.trigger(
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

        ``check_fleet`` (and everything it calls -- ``AutoMode``,
        ``RemediationLoop``) is unconverted synchronous code this pass
        (see docs/postgres-migration-plan.md's Phase 3 progress notes),
        so it's dispatched via ``asyncio.to_thread`` to avoid blocking
        the event loop for the tick's full duration.
        """
        click.echo(f"Starting vulnerability watcher (interval={self._interval}s)...", err=True)
        while True:
            try:
                events = self._consumer.poll_once()
                for event in events:
                    self._handle_event(event)
                await asyncio.to_thread(self.check_fleet)
                Path("/tmp/heartbeat").touch()
                record_tick(self._store, "vuln-watcher", success=True)
            except KeyboardInterrupt:
                click.echo("Vulnerability watcher stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("vuln-watch tick failed")
                click.echo(f"[vuln-watch] Error: {exc}", err=True)
                record_tick(self._store, "vuln-watcher", success=False, error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except KeyboardInterrupt:
                click.echo("Vulnerability watcher stopped.", err=True)
                break
