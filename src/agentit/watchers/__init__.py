"""Long-lived watcher agents (vuln-watcher, slo-tracker, drift-detector,
skill-learner, reassess-scheduler).

This package intentionally has shared telemetry helpers so every watcher
records the same "did the last tick succeed, and when did it last run"
signal for the portal's Agents/Schedules pages.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Touch /tmp/heartbeat at least this often while sleeping between ticks, so
# a liveness probe's staleness check reflects "is the process alive", not
# "did a tick just finish". Without this, any tick that completes (success
# or failure) is followed by a sleep of up to the watcher's own `--interval`
# (hours, for vuln-watcher/skill-learner) with nothing refreshing the
# heartbeat, so kubelet SIGKILLs the container ~15-19 minutes into every
# single sleep, forever -- see vuln-watcher's incident writeup for the full
# postgres-tick-timestamp evidence. Originally a vuln-watcher-only private
# method; pulled up here so every long-interval watcher (skill-learner
# included) can reuse the exact same fix instead of re-deriving it.
HEARTBEAT_REFRESH_SECONDS = 300


async def sleep_with_heartbeat(
    seconds: int,
    *,
    refresh_seconds: int = HEARTBEAT_REFRESH_SECONDS,
    heartbeat_path: str | Path = "/tmp/heartbeat",
) -> None:
    """Sleep for ``seconds``, touching ``heartbeat_path`` at least every
    ``refresh_seconds`` instead of only once before/after the whole sleep.
    Matters whenever a watcher's own tick interval exceeds its liveness
    probe's staleness window (e.g. vuln-watcher's 6h tick vs. a 900s probe,
    skill-learner's 24h tick vs. the same probe)."""
    path = Path(heartbeat_path)
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, refresh_seconds)
        await asyncio.sleep(chunk)
        path.touch()
        remaining -= chunk


async def record_tick(store: object | None, watcher_name: str, success: bool, error: str | None = None) -> None:
    """Record one watcher loop iteration: a tick-complete/tick-failed event plus a heartbeat.

    ``store`` is the ``AssessmentStore`` handed to the watcher's
    constructor, so both calls below are `await`ed.

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
