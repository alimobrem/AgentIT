"""Long-lived watcher agents (vuln-watcher, slo-tracker, drift-detector, skill-learner).

This package intentionally has shared telemetry helpers so every watcher
records the same "did the last tick succeed, and when did it last run"
signal for the portal's Agents/Schedules pages.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def record_tick(store: object | None, watcher_name: str, success: bool, error: str | None = None) -> None:
    """Record one watcher loop iteration: a tick-complete/tick-failed event plus a heartbeat.

    ``store`` is the async-compatible store handed to the watcher's
    constructor (``AsyncSQLiteStore`` or ``store_pg.AssessmentStore`` --
    never the raw sync ``AssessmentStore``, see docs/postgres-migration-
    plan.md), so both calls below are `await`ed.

    Best-effort — a store failure must never crash a watcher's main loop, so
    every exception here is caught and logged rather than propagated.
    """
    if success:
        try:
            import time as _time
            from agentit.portal.metrics import watcher_last_success_timestamp
            watcher_last_success_timestamp.labels(watcher=watcher_name).set(_time.time())
        except Exception:
            logger.warning("Failed to set watcher_last_success_timestamp for %s", watcher_name, exc_info=True)

    if store is None:
        return
    try:
        if success:
            await store.log_event(
                watcher_name, "tick-complete", None, "info",
                f"{watcher_name} tick completed successfully",
            )
        else:
            await store.log_event(
                watcher_name, "tick-failed", None, "error",
                f"{watcher_name} tick failed: {error}",
            )
        await store.agent_heartbeat(watcher_name)
    except Exception:
        logger.warning("Failed to record tick telemetry for %s", watcher_name, exc_info=True)
