"""Tests for durability features: dedup, circuit breaker, event buffer, thread limits."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from conftest import make_report, make_store


class TestWebhookDedup:
    def test_mark_and_check_processed(self):
        store = make_store()
        assert not store.webhook_already_processed("delivery-123")
        store.mark_webhook_processed("delivery-123")
        assert store.webhook_already_processed("delivery-123")

    def test_duplicate_mark_is_noop(self):
        store = make_store()
        store.mark_webhook_processed("delivery-456")
        store.mark_webhook_processed("delivery-456")  # no error
        assert store.webhook_already_processed("delivery-456")


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


class TestRemediationThreadLimits:
    def test_active_job_count(self):
        from agentit.remediation_loop import active_job_count
        assert isinstance(active_job_count(), int)


class TestDataExport:
    def test_export_all_returns_tables(self):
        store = make_store()
        data = store.export_all()
        assert "assessments" in data
        assert "events" in data
        assert "gates" in data
        assert isinstance(data["assessments"], list)


class TestHealthProbes:
    def test_healthz(self):
        from starlette.testclient import TestClient
        from agentit.portal.app import app
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readyz(self):
        from starlette.testclient import TestClient
        from agentit.portal.app import app
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
