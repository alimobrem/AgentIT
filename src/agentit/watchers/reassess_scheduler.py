"""Re-assessment scheduler agent — automatically re-Assesses apps on their
configured cadence (``apps.assessment_cadence``: daily/weekly/monthly/manual).

Before this watcher existed, nothing in AgentIT re-checked an onboarded app
on a timer at all -- see ``delivery.py``'s own "there's no periodic re-check
on a schedule" copy and the Schedules page's ``scheduled_operations`` table,
which is a DB-only reminder a human reads, never something a process acts
on (``portal/routes/schedules.py::create_schedule()``). This is the first
real mechanism: every tick, it asks the store which apps are due
(``AssessmentStore.get_apps_due_for_reassessment()``) and, for each one,
calls the exact same ``/api/webhook/assess`` route the manual Fleet
"Scan"/"Re-scan" buttons and ``RemediationLoop._assess()``
already use -- not a second, parallel assessment code path.

Runs cross-pod (like ``RemediationLoop``/``SkillLearner``), so it calls back
into the portal over HTTP via ``internal_webhook_client`` rather than
importing the assess pipeline directly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from agentit.internal_webhook_client import internal_webhook_client
from agentit.watchers import record_tick, sleep_with_heartbeat

logger = logging.getLogger(__name__)


class ReassessScheduler:
    """Long-lived agent that re-Assesses apps automatically once their
    configured cadence has elapsed since their last assessment.
    """

    def __init__(
        self,
        store: object,
        interval: int = 3600,
        portal_url: str | None = None,
        timeout: int = 120,
    ) -> None:
        self._store = store
        self._interval = interval
        self._portal = (portal_url or os.environ.get("AGENTIT_PORTAL_URL", "http://localhost:8080")).rstrip("/")
        self._client = internal_webhook_client(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _trigger_assess(self, repo_url: str, criticality: str) -> dict:
        try:
            resp = await self._client.post(
                f"{self._portal}/api/webhook/assess",
                json={"repo_url": repo_url, "criticality": criticality},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    async def check_due_apps(self) -> list[dict]:
        """One tick: find every app due for automatic re-assessment and
        trigger it via the shared ``/api/webhook/assess`` route.

        ``self._store`` is the ``AssessmentStore`` handed in by ``cli.py``'s
        ``reassess_watch`` command, so every store call here is `await`ed
        directly.
        """
        due = await self._store.get_apps_due_for_reassessment()
        click.echo(f"[reassess-watch] {len(due)} app(s) due for automatic re-assessment", err=True)

        results: list[dict] = []
        for app in due:
            repo_url = app["repo_url"]
            repo_name = app["repo_name"]
            cadence = app["assessment_cadence"]
            criticality = app.get("criticality") or "medium"
            click.echo(
                f"[reassess-watch] Re-assessing {repo_name} (cadence={cadence}, "
                f"last assessed {app['last_assessed_at']})...",
                err=True,
            )
            result = await self._trigger_assess(repo_url, criticality)
            if "error" in result:
                logger.warning("Automatic re-assessment failed for %s: %s", repo_name, result["error"])
                click.echo(f"[reassess-watch] Failed for {repo_name}: {result['error']}", err=True)
                await self._store.log_event(
                    "reassess-scheduler", "auto-reassess-failed", repo_name, "warning",
                    f"Automatic {cadence} re-assessment failed: {result['error']}",
                )
            elif result.get("status") == "duplicate":
                # /api/webhook/assess's own dedup guard (webhooks.py's
                # claim_webhook) claimed this call as a duplicate of one
                # already in flight for this exact repo_url+criticality --
                # nothing more to do this tick, the in-flight call already
                # covers it.
                click.echo(f"[reassess-watch] {repo_name} already has an assessment in flight, skipping", err=True)
            else:
                score = result.get("overall_score")
                click.echo(f"[reassess-watch] {repo_name} re-assessed: {score}/100", err=True)
                await self._store.log_event(
                    "reassess-scheduler", "auto-reassess-triggered", repo_name, "info",
                    f"Automatic {cadence} re-assessment complete: {score}/100",
                    correlation_id=result.get("assessment_id"),
                )
            results.append({"repo_name": repo_name, **result})
        return results

    async def run(self) -> None:
        """Main loop: check for due apps, trigger re-assessment, sleep."""
        click.echo(f"Starting re-assessment scheduler (interval={self._interval}s)...", err=True)
        while True:
            try:
                await self.check_due_apps()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, "reassess-scheduler", success=True)
            except KeyboardInterrupt:
                click.echo("Re-assessment scheduler stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("reassess-watch tick failed")
                click.echo(f"[reassess-watch] Error: {exc}", err=True)
                await record_tick(self._store, "reassess-scheduler", success=False, error=str(exc))

            try:
                await sleep_with_heartbeat(self._interval)
            except KeyboardInterrupt:
                click.echo("Re-assessment scheduler stopped.", err=True)
                break
