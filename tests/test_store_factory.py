"""Tests for the storage-agnostic async store factory (Phase 3 enabling
infrastructure, docs/postgres-migration-plan.md).

The sqlite-backend tests below run unconditionally (no flags/containers
needed) and are the real safety net for this pass: they assert that
``create_store()``'s default path is byte-for-byte behavior-preserving vs.
calling ``store.py`` directly. The postgres-backend test reuses the
``postgres_dsn`` fixture from ``test_store_pg.py`` and is gated the same way
(``--run-postgres-tests``), per docs/postgres-migration-plan.md §8.
"""
from __future__ import annotations

import os

import pytest

from agentit.portal.store import AssessmentStore
from agentit.portal.store_factory import AsyncSQLiteStore, create_store
from conftest import make_report
from test_store_pg import postgres_dsn  # noqa: F401 -- reused fixture, see module docstring


async def test_create_store_defaults_to_sqlite_when_backend_unset(monkeypatch):
    monkeypatch.delenv("AGENTIT_DB_BACKEND", raising=False)
    store = await create_store(":memory:")
    assert isinstance(store, AsyncSQLiteStore)
    assert isinstance(store.raw, AssessmentStore)


async def test_create_store_explicit_sqlite_backend(monkeypatch):
    monkeypatch.setenv("AGENTIT_DB_BACKEND", "sqlite")
    store = await create_store(":memory:")
    assert isinstance(store, AsyncSQLiteStore)


async def test_async_sqlite_store_save_and_get_round_trip_matches_sync(monkeypatch):
    """The core behavior-preservation claim: awaiting the async facade must
    produce identical results to calling the sync store directly."""
    monkeypatch.delenv("AGENTIT_DB_BACKEND", raising=False)
    store = await create_store(":memory:")
    report = make_report(repo_name="factory-test-app")

    assessment_id = await store.save(report)
    via_async = await store.get(assessment_id)
    via_sync = store.raw.get(assessment_id)

    assert via_async == via_sync
    assert via_async.repo_name == "factory-test-app"


async def test_async_sqlite_store_non_callable_attribute_passthrough():
    store = AsyncSQLiteStore(":memory:")
    assert store.raw._db_path == ":memory:"


async def test_async_sqlite_store_propagates_exceptions():
    """A failure inside the wrapped sync call must surface as a normal
    raised exception from the coroutine, not get swallowed by the thread hop."""
    store = AsyncSQLiteStore(":memory:")
    with pytest.raises(AttributeError):
        await store.save(None)  # report=None has no .repo_url -> AttributeError inside save()


@pytest.mark.postgres
async def test_create_store_postgres_backend_returns_store_pg_instance(monkeypatch, postgres_dsn):
    from agentit.portal import store_pg

    monkeypatch.setenv("AGENTIT_DB_BACKEND", "postgres")
    monkeypatch.setenv("AGENTIT_DB_DSN", postgres_dsn)
    store = await create_store(min_size=1, max_size=2)
    try:
        assert isinstance(store, store_pg.AssessmentStore)
        assert not hasattr(store, "raw")
    finally:
        await store.close()
