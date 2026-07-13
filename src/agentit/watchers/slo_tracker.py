"""SLO tracker agent — monitors SLO health and recommends rollbacks on breach."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from agentit.consumer import EventConsumer
from agentit.events import EventPublisher, TOPIC_ALERTS
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
        store: object,
        consumer: EventConsumer,
        interval: int = 300,
    ) -> None:
        self._publisher = publisher
        self._store = store
        self._consumer = consumer
        self._interval = interval

    async def check_once(self) -> int:
        """Check all assessments for SLO breaches.

        ``self._store`` is the async-compatible store handed in by
        ``cli.py``'s ``slo_track`` command -- every store call below is
        `await`ed directly. The one genuinely blocking call this tick makes
        (``collect_slo``, synchronous ``kubernetes``-client I/O) is
        narrowly wrapped in ``asyncio.to_thread`` at its call site in
        ``_collect_fresh_values``, not the whole method.

        Returns the number of apps with at least one breached SLO.
        """
        assessments = await self._store.list_all()
        breached_apps = 0

        for a in assessments:
            slos = await self._store.list_slos(a["id"])
            await self._collect_fresh_values(a, slos)
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
                await self._recommend_rollback(a, app_breaches)

        click.echo(
            f"[slo-track] Checked {len(assessments)} apps, {breached_apps} with breaches",
            err=True,
        )
        return breached_apps

    async def _collect_fresh_values(self, assessment: dict, slos: list[dict]) -> None:
        """Collect a fresh metric value for each SLO and update its status in-place.

        SLOs whose metric type has no cluster-side collector (e.g.
        latency_p99_ms) are logged and left with their prior status rather
        than silently skipped.
        """
        namespace = assessment["repo_name"]
        for slo in slos:
            value = await asyncio.to_thread(collect_slo, slo["metric_name"], namespace)
            if value is None:
                logger.debug(
                    "[slo-track] Could not collect %r for %s -- leaving prior status",
                    slo["metric_name"], namespace,
                )
                continue
            status = "breached" if is_breached(slo["metric_name"], value, slo["target_value"]) else "met"
            await self._store.update_slo(slo["id"], value, status)
            slo["current_value"] = value
            slo["status"] = status

    async def _recommend_rollback(self, assessment: dict, breaches: list[dict]) -> None:
        """If a recent apply exists for this assessment, create a rollback gate."""
        apply_result = await self._store.get_apply_results(assessment["id"])
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
        await self._store.create_gate(
            assessment["id"],
            "rollback-review",
            f"SLO breach detected for {repo_name} after recent apply. "
            f"Breached: {breach_names}. "
            f"Review and decide: rollback or investigate.",
        )
        click.echo(f"[slo-track] ROLLBACK RECOMMENDED: {repo_name}", err=True)

    async def run(self) -> None:
        """Main loop: drain events, check SLOs, sleep.

        ``check_once`` is now a genuine coroutine -- it's `await`ed
        directly rather than dispatched via ``asyncio.to_thread`` (which
        would just add a redundant thread hop), since its one truly
        blocking call (``collect_slo``) is already narrowly wrapped in
        ``asyncio.to_thread`` internally, in ``_collect_fresh_values``.
        """
        click.echo(f"Starting SLO tracker (interval={self._interval}s)...", err=True)
        while True:
            try:
                self._consumer.poll_once()
                await self.check_once()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, "slo-tracker", success=True)
            except KeyboardInterrupt:
                click.echo("SLO tracker stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("slo-track tick failed")
                click.echo(f"[slo-track] Error: {exc}", err=True)
                await record_tick(self._store, "slo-tracker", success=False, error=str(exc))

            try:
                await asyncio.sleep(self._interval)
            except KeyboardInterrupt:
                click.echo("SLO tracker stopped.", err=True)
                break
