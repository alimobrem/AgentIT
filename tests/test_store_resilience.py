"""Fault-injection tests for `AssessmentStore`'s resilience against a
*wedged* (not fully down, just stuck) Postgres -- found during the
2026-07-18 resilience audit (see `docs/resilience-audit-2026-07-18.md`).

Unlike `test_kube_breaker.py`/`test_llm_breaker.py` (which mock the failing
dependency, since a real cluster/LLM call is never appropriate in tests),
the tests below use a *real* Postgres (the same session-shared instance
`tests/conftest.py` provides) and inject the fault at the SQL level with
`pg_sleep()` -- a genuine, reproducible way to make a real connection
"wedged" without needing to simulate a network partition. This proves the
actual code path (`AssessmentStore.create()`'s `command_timeout`/
`connect_timeout`), not a mocked stand-in for it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agentit.portal.store import AssessmentStore
from conftest import _resolve_postgres_dsn


class TestCommandTimeoutBoundsWedgedQueries:
    """A store built with a short `command_timeout` must turn a wedged
    query into a fast, clear error instead of hanging -- the exact
    "resource exhaustion via a stuck dependency" scenario this fix closes
    (see the audit doc's "External dependency resilience: Postgres"
    finding)."""

    async def test_wedged_query_times_out_instead_of_hanging(self):
        dsn = _resolve_postgres_dsn()
        if dsn is None:
            pytest.skip("no Postgres available for this test session")

        store = await AssessmentStore.create(
            dsn, min_size=1, max_size=2, command_timeout=1.0, connect_timeout=5.0,
        )
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                # Simulates a genuinely wedged query (a lock wait, a runaway
                # statement on someone else's connection, ...) -- before the
                # fix this had no bound at all and would hang indefinitely.
                await store._pool.execute("SELECT pg_sleep(30)")
            elapsed = time.monotonic() - start

            # Generous upper bound (the real `command_timeout` is 1.0s) --
            # this is the actual regression assertion: bounded, not "30s
            # (pg_sleep's argument) or more".
            assert elapsed < 5.0
        finally:
            await store.close()

    async def test_pool_recovers_after_a_timeout(self):
        """A timed-out connection must not poison the pool -- the next,
        legitimate query on the same pool must still succeed. Without this,
        bounding the timeout would just convert "hangs forever" into
        "permanently broken after the first slow query", which is not an
        improvement."""
        dsn = _resolve_postgres_dsn()
        if dsn is None:
            pytest.skip("no Postgres available for this test session")

        store = await AssessmentStore.create(
            dsn, min_size=1, max_size=2, command_timeout=1.0, connect_timeout=5.0,
        )
        try:
            with pytest.raises(TimeoutError):
                await store._pool.execute("SELECT pg_sleep(30)")

            result = await store._pool.fetchval("SELECT 1")
            assert result == 1
        finally:
            await store.close()

    async def test_concurrent_requests_are_not_all_blocked_by_one_wedged_query(self):
        """Regression guard for the pool-exhaustion cascade: with a bound
        in place, a single wedged query only ties up its own connection for
        `command_timeout` seconds, not the whole `max_size`-connection pool
        -- other, unrelated concurrent queries must still complete quickly
        rather than queueing behind the wedge indefinitely."""
        dsn = _resolve_postgres_dsn()
        if dsn is None:
            pytest.skip("no Postgres available for this test session")

        store = await AssessmentStore.create(
            dsn, min_size=2, max_size=4, command_timeout=2.0, connect_timeout=5.0,
        )
        try:
            async def wedge():
                try:
                    await store._pool.execute("SELECT pg_sleep(30)")
                except TimeoutError:
                    pass

            start = time.monotonic()
            wedge_task = asyncio.create_task(wedge())
            await asyncio.sleep(0.1)  # let the wedge actually claim a connection first
            result = await store._pool.fetchval("SELECT 42")
            elapsed = time.monotonic() - start

            assert result == 42
            assert elapsed < 2.0  # answered well before the wedge's own 2s timeout even fires
            await wedge_task
        finally:
            await store.close()
