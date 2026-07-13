"""Tests for the generic remediation dispatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentit.agents.hardening import patch_base_image
from agentit.portal.app import app, get_store
from agentit.portal.store_factory import AsyncSQLiteStore
from agentit.remediation.dispatcher import RemediationDispatcher
from agentit.remediation.registry import lookup
from conftest import make_report, make_store


# ── Registry Lookup ─────────────────────────────────────────────────


class TestRegistryLookup:
    def test_exact_match(self):
        assert lookup("container") == ("hardening", "_generate_containerfile")

    def test_substring_match(self):
        assert lookup("container security") == ("hardening", "_generate_containerfile")

    def test_unknown_category_returns_none(self):
        assert lookup("banana") is None

    def test_all_registered_categories(self):
        for cat in ("network", "scanning", "sbom", "pipeline", "metrics", "tracing", "base_image"):
            result = lookup(cat)
            assert result is not None, f"No fix registered for '{cat}'"


# ── patch_base_image ────────────────────────────────────────────────


class TestPatchBaseImage:
    def test_patches_python_base(self):
        content = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nUSER 1001\n"
        result = patch_base_image(content, "python")
        assert result is not None
        assert "ubi9/python-312" in result
        assert "WORKDIR /app" in result
        assert "USER 1001" in result

    def test_patches_node_base(self):
        result = patch_base_image("FROM node:20-slim\nWORKDIR /app\n", "javascript")
        assert result is not None
        assert "ubi9/nodejs-20" in result

    def test_patches_go_base(self):
        result = patch_base_image("FROM golang:1.22\nWORKDIR /app\n", "go")
        assert result is not None
        assert "ubi-minimal" in result

    def test_patches_java_base(self):
        result = patch_base_image("FROM openjdk:21\nWORKDIR /app\n", "java")
        assert result is not None
        assert "ubi9/openjdk-21" in result

    def test_preserves_ubi_base(self):
        content = "FROM registry.access.redhat.com/ubi9/python-312:latest\nWORKDIR /app\n"
        assert patch_base_image(content, "python") is None

    def test_preserves_multi_stage_build(self):
        content = "FROM golang:1.22 AS builder\nRUN go build\nFROM python:3.12\nCOPY --from=builder /app /app\n"
        result = patch_base_image(content, "python")
        assert result is not None
        assert "golang:1.22 AS builder" in result
        assert "ubi9/python-312" in result

    def test_returns_none_for_no_from(self):
        assert patch_base_image("WORKDIR /app\n", "python") is None


# ── Dispatcher ──────────────────────────────────────────────────────


class TestDispatcher:
    @pytest.fixture
    def store(self):
        return make_store()

    def test_dispatch_unknown_category(self, store):
        dispatcher = RemediationDispatcher(store)
        result = dispatcher.dispatch("fake-id", "banana")
        assert result["error"]
        assert result["files"] == []

    def test_dispatch_missing_assessment(self, store):
        dispatcher = RemediationDispatcher(store)
        result = dispatcher.dispatch("nonexistent", "network")
        assert "not found" in result["error"]

    def test_dispatch_network_generates_policy(self, store, tmp_path):
        from agentit.models import DimensionScore, Finding, Severity
        report = make_report(scores=[
            DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="No NetworkPolicy", recommendation="Add one"),
            ]),
        ])
        aid = store.save(report)
        dispatcher = RemediationDispatcher(store)
        result = dispatcher.dispatch(aid, "network")
        assert result["error"] is None
        assert result["agent"] == "hardening"
        assert len(result["files"]) > 0
        assert any("network" in f["path"].lower() for f in result["files"])

    def test_dispatch_sbom_generates_task(self, store):
        from agentit.models import DimensionScore, Finding, Severity
        report = make_report(scores=[
            DimensionScore(dimension="compliance", score=30, max_score=100, findings=[
                Finding(category="sbom supply chain", severity=Severity.medium,
                        description="No SBOM", recommendation="Add SBOM"),
            ]),
        ])
        aid = store.save(report)
        dispatcher = RemediationDispatcher(store)
        result = dispatcher.dispatch(aid, "sbom")
        assert result["error"] is None
        assert result["agent"] == "compliance"
        assert any("sbom" in f["path"].lower() for f in result["files"])


# ── Webhook Integration ────────────────────────────────────────────


@pytest.fixture
def _override_store():
    test_store = make_store()
    async_store = AsyncSQLiteStore.wrap(test_store)
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store):
        yield test_store


@pytest.fixture
def client():
    return TestClient(app)


class TestFindingWebhook:
    def test_rejects_missing_category(self, client, _override_store):
        resp = client.post("/api/webhook/finding", json={"app_name": "test"})
        assert resp.status_code == 400

    def test_alert_only_for_unknown_app(self, client, _override_store):
        resp = client.post("/api/webhook/finding", json={
            "app_name": "no-such-app",
            "category": "network",
            "description": "test",
        })
        assert resp.status_code == 200
        assert resp.json()["action"] == "alert-only"

    def test_gates_fix_when_automode_off(self, client, _override_store):
        from agentit.models import DimensionScore, Finding, Severity
        store = _override_store
        report = make_report(
            repo_name="gated-app",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="No NetworkPolicy", recommendation="Add one"),
            ])],
        )
        store.save(report)
        resp = client.post("/api/webhook/finding", json={
            "app_name": "gated-app",
            "category": "network",
            "description": "Missing NetworkPolicy",
            "severity": "high",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "gated"
        assert data["files_generated"] > 0
        assert data["gate_id"]
