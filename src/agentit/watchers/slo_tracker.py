"""SLO tracker agent — monitors SLO health and recommends rollbacks on breach."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from agentit.consumer import EventConsumer
from agentit.events import EventPublisher, TOPIC_ALERTS
from agentit.portal.store import AssessmentStore
from agentit.slo_collector import collect_slo, is_breached
from agentit.watchers import record_tick

logger = logging.getLogger(__name__)


class SloTracker:
    """Long-lived agent that checks SLO status for all assessed apps and
    publishes alerts (+ rollback recommendations) when breaches are detected.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        store: AssessmentStore,
        consumer: EventConsumer,
        interval: int = 300,
    ) -> None:
        self._publisher = publisher
        self._store = store
        self._consumer = consumer
        self._interval = interval

    def check_once(self) -> int:
        """Check all assessments for SLO breaches.

        Returns the number of apps with at least one breached SLO.
        """
        assessments = self._store.list_all()
        breached_apps = 0

        for a in assessments:
            slos = self._store.list_slos(a["id"])
            self._collect_fresh_values(a, slos)
            app_breaches = [s for s in slos if s["status"] == "breached"]

            for slo in app_breaches:
                self._publisher.publish(
                    TOPIC_ALERTS,
                    agent_id="slo-tracker",
                    action="slo-breach",
                    target_app=a["repo_name"],
                    severity="critical",
                    summary=(
                        f"SLO breached: {slo['metric_name']} "
                        f"(target={slo['target_value']}, current={slo['current_value']})"
                    ),
                )

            if app_breaches:
                breached_apps += 1
                self._recommend_rollback(a, app_breaches)

        click.echo(
            f"[slo-track] Checked {len(assessments)} apps, {breached_apps} with breaches",
            err=True,
        )
        return breached_apps

    def _collect_fresh_values(self, assessment: dict, slos: list[dict]) -> None:
        """Collect a fresh metric value for each SLO and update its status in-place.

        SLOs whose metric type has no cluster-side collector (e.g.
        latency_p99_ms) are logged and left with their prior status rather
        than silently skipped.
        """
        namespace = assessment["repo_name"]
        for slo in slos:
            value = collect_slo(slo["metric_name"], namespace)
            if value is None:
                logger.debug(
                    "[slo-track] Could not collect %r for %s -- leaving prior status",
                    slo["metric_name"], namespace,
                )
                continue
            status = "breached" if is_breached(slo["metric_name"], value, slo["target_value"]) else "met"
            self._store.update_slo(slo["id"], value, status)
            slo["current_value"] = value
            slo["status"] = status

    def _recommend_rollback(self, assessment: dict, breaches: list[dict]) -> None:
        """If a recent apply exists for this assessment, create a rollback gate."""
        apply_result = self._store.get_apply_results(assessment["id"])
        if not (apply_result and apply_result.get("applied")):
            return

        repo_name = assessment["repo_name"]
        breach_names = ", ".join(s["metric_name"] for s in breaches)

        self._publisher.publish(
            TOPIC_ALERTS,
            agent_id="slo-tracker",
            action="rollback-recommended",
            target_app=repo_name,
            severity="critical",
            summary=(
                f"SLO breach after recent apply — consider rollback: "
                f"kubectl argo rollouts undo {repo_name}"
            ),
        )
        self._store.create_gate(
            assessment["id"],
            "rollback-review",
            f"SLO breach detected for {repo_name} after recent apply. "
            f"Breached: {breach_names}. "
            f"Review and decide: rollback or investigate.",
        )
        click.echo(f"[slo-track] ROLLBACK RECOMMENDED: {repo_name}", err=True)

    async def run(self) -> None:
        """Main loop: drain events, check SLOs, sleep.

        The tick body (``poll_once``/``check_once``) is unconverted
        synchronous code this pass (see
        docs/postgres-migration-plan.md's Phase 3 progress notes), so it
        still runs inline here -- only this outer loop is async-shaped for
        now (``await asyncio.sleep`` instead of ``time.sleep``), per the
        plan's §5.
        """
        click.echo(f"Starting SLO tracker (interval={self._interval}s)...", err=True)
        while True:
            try:
                self._consumer.poll_once()
                self.check_once()
                Path("/tmp/heartbeat").touch()
                record_tick(self._store, "slo-tracker", success=True)
            except KeyboardInterrupt:
                click.echo("SLO tracker stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("slo-track tick failed")
                click.echo(f"[slo-track] Error: {exc}", err=True)
                record_tick(self._store, "slo-tracker", success=False, error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except KeyboardInterrupt:
                click.echo("SLO tracker stopped.", err=True)
                break
