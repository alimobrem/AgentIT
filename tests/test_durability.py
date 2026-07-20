"""Tests for durability features: dedup, circuit breaker, event buffer, thread limits."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from conftest import make_report, make_store


class TestCircuitBreaker:
    def test_starts_closed(self):
        from agentit.portal.helpers import CircuitBreaker
        cb = CircuitBreaker("test", threshold=3, reset_after=1)
        assert not cb.is_open

    def test_opens_after_threshold(self):
        from agentit.portal.helpers import CircuitBreaker
        cb = CircuitBreaker("test", threshold=2, reset_after=60)
        cb.record_failure()
        assert not cb.is_open
        cb.record_failure()
        assert cb.is_open

    def test_resets_after_timeout(self):
        from agentit.portal.helpers import CircuitBreaker
        cb = CircuitBreaker("test", threshold=1, reset_after=0.1)
        cb.record_failure()
        assert cb.is_open
        time.sleep(0.15)
        assert not cb.is_open

    def test_success_resets_failures(self):
        from agentit.portal.helpers import CircuitBreaker
        cb = CircuitBreaker("test", threshold=3, reset_after=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert not cb.is_open  # reset by success


class TestCircuitBreakerStateAccessor:
    def test_get_circuit_breaker_states_reports_llm_and_kube(self):
        from agentit.portal.helpers import get_circuit_breaker_states, llm_breaker, kube_breaker

        llm_breaker._failures = 0
        kube_breaker._failures = 0
        states = get_circuit_breaker_states()
        assert set(states) == {"llm", "kube"}
        assert states["llm"]["open"] is False
        assert states["llm"]["failures"] == 0

    def test_get_circuit_breaker_states_reflects_open_breaker(self):
        from agentit.portal.helpers import get_circuit_breaker_states, kube_breaker

        for _ in range(kube_breaker._threshold):
            kube_breaker.record_failure()
        try:
            states = get_circuit_breaker_states()
            assert states["kube"]["open"] is True
        finally:
            kube_breaker.record_success()  # don't leak state into other tests

    def test_refresh_circuit_breaker_gauge_sets_prometheus_gauge(self):
        from agentit.portal.helpers import kube_breaker
        from agentit.portal.metrics import circuit_breaker_open, refresh_circuit_breaker_gauge

        kube_breaker.record_success()
        refresh_circuit_breaker_gauge()
        assert circuit_breaker_open.labels(name="kube")._value.get() == 0.0


class TestEventBufferBacklog:
    def test_get_buffer_backlog_counts_buffered_events(self, tmp_path):
        from agentit.events import EventPublisher

        pub = EventPublisher.__new__(EventPublisher)
        pub._buffer_db = str(tmp_path / "event-buffer.db")
        pub._init_buffer_db()
        assert pub.get_buffer_backlog() == 0

        pub._buffer_locally("agentit-events", {"action": "test"})
        assert pub.get_buffer_backlog() == 1

    def test_get_buffer_backlog_missing_db_returns_zero(self, tmp_path):
        from agentit.events import EventPublisher

        pub = EventPublisher.__new__(EventPublisher)
        pub._buffer_db = str(tmp_path / "does-not-exist" / "event-buffer.db")
        assert pub.get_buffer_backlog() == 0


class TestRefreshDbMetrics:
    async def test_refresh_db_metrics_sets_gauges_without_raising(self):
        """Exercised via `app.py::_background_maintenance` against the real
        store -- regression guard for the bug where that loop fell back to
        handing this helper a nonexistent `.raw` instead of genuinely
        awaiting it."""
        from agentit.portal.metrics import refresh_db_metrics

        store = await make_store()
        await store.save(make_report())
        await refresh_db_metrics(store)  # must not raise even with no Kafka configured


class TestDataExport:
    async def test_export_all_returns_tables(self):
        store = await make_store()
        data = await store.export_all()
        assert "assessments" in data
        assert "events" in data
        assert isinstance(data["assessments"], list)


class TestHealthProbes:
    async def test_healthz(self):
        from httpx import ASGITransport, AsyncClient
        from agentit.portal.app import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readyz(self):
        from httpx import ASGITransport, AsyncClient
        from agentit.portal.app import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            resp = await client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
