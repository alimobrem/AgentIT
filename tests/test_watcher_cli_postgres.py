"""End-to-end proof that the 4 watcher CLI commands (`vuln-watch`,
`slo-track`, `drift-detect`, `learn-watch`) genuinely support the async
Postgres-backed store directly -- the real blocker found and rolled back
during the cutover attempt documented in docs/postgres-migration-plan.md's
"real cutover attempted and rolled back" section (``store.raw``, which
``store_pg.AssessmentStore`` deliberately does not have).

Each test below sets ``AGENTIT_DB_BACKEND=postgres``/``AGENTIT_DB_DSN`` and
invokes the *actual* CLI coroutine (unwrapped from the `@_run_async`/Click
plumbing via `.__wrapped__`, since `_run_async`'s own `asyncio.run()` can't
be nested inside pytest-asyncio's already-running loop) end to end: real
``create_store()`` construction against a real Postgres container, real
watcher/`EventConsumer` construction with that store (no `.raw`), one real
tick, and real reads/writes verified afterward against the same database
via a second, independent connection -- not just "it didn't raise
AttributeError".

Requires a real Postgres instance; gated the same way as
``tests/test_store_pg.py`` (``--run-postgres-tests``, reusing its
``postgres_dsn``/container-lifecycle fixture).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from agentit.cli import main
from agentit.portal import store_pg
from test_store_pg import postgres_dsn  # noqa: F401 -- reused fixture, see module docstring

pytestmark = pytest.mark.postgres


def _unwrapped(command_name: str):
    """Return the original `async def` command coroutine, bypassing the
    `@_run_async` decorator's `asyncio.run()` (which can't nest inside
    pytest-asyncio's own running loop) -- `functools.wraps` inside
    `_run_async` leaves `.__wrapped__` pointing at it."""
    return main.commands[command_name].callback.__wrapped__


@pytest.fixture
async def pg_store(postgres_dsn, monkeypatch):
    """Point AGENTIT_DB_BACKEND/AGENTIT_DB_DSN at the real container for the
    CLI command under test, and hand back an independent, truncated-table
    connection (mirroring test_store_pg.py's own `store` fixture) for
    verifying what the CLI command actually wrote."""
    monkeypatch.setenv("AGENTIT_DB_BACKEND", "postgres")
    monkeypatch.setenv("AGENTIT_DB_DSN", postgres_dsn)

    verifier = await store_pg.AssessmentStore.create(postgres_dsn, min_size=1, max_size=3)
    async with verifier._pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE assessments, onboarding_results, events, gates, remediations, "
            "agent_registry, slos, apply_results, settings, remediation_jobs, "
            "scheduled_operations, processed_webhooks, agent_feedback, "
            "skill_effectiveness, suppressed_checks, skill_inventory_snapshots, "
            "agent_runs, check_results CASCADE"
        )
    yield verifier
    await verifier.close()


async def test_vuln_watch_ticks_once_against_real_postgres(pg_store):
    coro = _unwrapped("vuln-watch")

    with patch("agentit.watchers.vuln_watcher.asyncio.sleep", side_effect=KeyboardInterrupt):
        await coro(interval=1)  # must not raise AttributeError on store.raw

    events = await pg_store.list_events()
    assert any(e["action"] == "tick-complete" and e["agent_id"] == "vuln-watcher" for e in events)
    agents = await pg_store.list_agents()
    assert any(a["agent_name"] == "vuln-watcher" for a in agents)


async def test_slo_track_ticks_once_against_real_postgres(pg_store):
    """Also proves the store round-trips real assessment/SLO data: seeds an
    assessment + SLO directly via the verifier connection, then confirms
    the watcher's own tick (against a *separate* store/pool instance
    pointed at the same DSN) reads and updates it."""
    from agentit.models import (
        ArchitectureInfo, AssessmentReport, DimensionScore, Finding,
        Language, Severity, StackInfo,
    )
    from datetime import datetime, timezone

    report = AssessmentReport(
        repo_url="https://github.com/org/pg-slo-app",
        repo_name="pg-slo-app",
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(languages=[Language(name="python", file_count=1, percentage=100.0)],
                         frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(service_count=1, architecture_style="monolith",
                                       has_api=True, api_style="REST", external_dependencies=[]),
        scores=[DimensionScore(dimension="security", score=80, max_score=100,
                                findings=[Finding(category="test", severity=Severity.low,
                                                   description="d", recommendation="r")])],
        criticality="medium", summary="s", remediation_plan=[],
    )
    aid = await pg_store.save(report)
    await pg_store.save_slo(aid, "availability", 99.9)

    coro = _unwrapped("slo-track")
    with patch("agentit.watchers.slo_tracker.collect_slo", return_value=99.99), \
         patch("agentit.watchers.slo_tracker.asyncio.sleep", side_effect=KeyboardInterrupt):
        await coro(interval=1)  # must not raise AttributeError on store.raw

    slos = await pg_store.list_slos(aid)
    assert slos[0]["current_value"] == 99.99
    assert slos[0]["status"] == "met"

    events = await pg_store.list_events()
    assert any(e["action"] == "tick-complete" and e["agent_id"] == "slo-tracker" for e in events)


async def test_drift_detect_ticks_once_against_real_postgres(pg_store):
    coro = _unwrapped("drift-detect")

    with patch("agentit.watchers.drift_detector.kube.list_custom_resources", return_value=None), \
         patch("agentit.watchers.drift_detector.asyncio.sleep", side_effect=KeyboardInterrupt):
        await coro(interval=1)  # must not raise AttributeError on store.raw

    events = await pg_store.list_events()
    assert any(e["action"] == "tick-complete" and e["agent_id"] == "drift-detector" for e in events)


async def test_learn_watch_ticks_once_against_real_postgres(pg_store):
    coro = _unwrapped("learn-watch")

    with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
         patch("agentit.watchers.skill_learner.asyncio.sleep", side_effect=KeyboardInterrupt):
        await coro(interval=1, limit=3, llm_model=None)  # must not raise AttributeError on store.raw

    events = await pg_store.list_events()
    assert any(e["action"] == "tick-complete" and e["agent_id"] == "skill-learner" for e in events)


async def test_consume_dispatches_blocking_loop_off_event_loop_with_postgres_store(pg_store):
    """`consume` has no Kafka available in this test env, so `EventConsumer`
    is never `.connected` and the command exits before touching the
    blocking loop -- this proves `create_store()` + `EventConsumer(store=...)`
    construction against the real Postgres backend succeeds without
    `.raw`, which is the specific piece this pass changed for this
    command (the blocking `consume()` loop itself is exercised, with a
    mocked/no-Kafka consumer, in tests/test_consumer.py)."""
    coro = _unwrapped("consume")

    with pytest.raises(SystemExit) as exc_info:
        await coro(topics="agentit-events", group_id="agentit-consumers")
    assert exc_info.value.code == 1
