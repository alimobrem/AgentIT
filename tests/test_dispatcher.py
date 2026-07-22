"""Tests for the generic remediation dispatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app, get_store
from agentit.remediation.base_image import patch_base_image
from agentit.remediation.dispatcher import RemediationDispatcher
from agentit.remediation.registry import lookup
from conftest import make_async_store, make_report, make_store


# ── Registry Lookup ─────────────────────────────────────────────────


class TestRegistryLookup:
    def test_exact_match(self):
        assert lookup("container") == ("security", "containerfile")

    def test_substring_match(self):
        assert lookup("container security") == ("security", "containerfile")

    def test_unknown_category_returns_none(self):
        assert lookup("banana") is None

    def test_all_registered_categories(self):
        for cat in ("network", "scanning", "sbom", "pipeline", "metrics", "tracing", "base_image"):
            result = lookup(cat)
            assert result is not None, f"No fix registered for '{cat}'"

    def test_rbac_autoscaling_monitoring_resolve(self):
        """Added for auto_delivery.py's validate/fix loop, which dispatches
        by exactly these category names (property_verifier.py's check
        names, lowercased) -- none of the substring keys above happened to
        match any of the three (e.g. "metrics" is not a substring of
        "monitoring"), so these previously resolved to None despite a real
        matching skill existing for each."""
        assert lookup("rbac") == ("security", "rbac")
        assert lookup("autoscaling") == ("infrastructure", "hpa")
        assert lookup("monitoring") == ("observability", "service-monitor")

    def test_scaling_and_quota_analyzer_categories_resolve(self):
        """ha_dr / infrastructure analyzers emit these exact category names
        (pinky open findings). They must resolve without relying on the
        "autoscaling" substring bridge or trigger-only fallback."""
        assert lookup("scaling") == ("infrastructure", "hpa")
        assert lookup("quota") == ("infrastructure", "resourcequota")
        # Detect-only today — no remediation skill registered yet.
        assert lookup("eol") is None
        assert lookup("migration") is None


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
    async def store(self):
        return await make_async_store()

    async def test_dispatch_unknown_category(self, store):
        async_store, _raw = store
        dispatcher = RemediationDispatcher(async_store)
        result = await dispatcher.dispatch("fake-id", "banana")
        assert result["error"]
        assert result["files"] == []

    async def test_dispatch_missing_assessment(self, store):
        async_store, _raw = store
        dispatcher = RemediationDispatcher(async_store)
        result = await dispatcher.dispatch("nonexistent", "network")
        assert "not found" in result["error"]

    async def test_dispatch_network_generates_policy(self, store, tmp_path):
        from agentit.models import DimensionScore, Finding, Severity
        async_store, raw = store
        report = make_report(scores=[
            DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="No NetworkPolicy", recommendation="Add one"),
            ]),
        ])
        aid = await raw.save(report)
        dispatcher = RemediationDispatcher(async_store)
        result = await dispatcher.dispatch(aid, "network")
        assert result["error"] is None
        assert result["agent"] == "security"
        assert len(result["files"]) > 0
        assert any("network" in f["path"].lower() for f in result["files"])

    async def test_dispatch_rbac_generates_rbac_manifest(self, store):
        """Real, deterministic (template-mode, no LLM) generation for the
        registry entry added alongside auto_delivery.py -- proves the fix
        the validate/fix loop dispatches for a failed RBAC property check
        actually produces the ServiceAccount/Role/RoleBinding
        property_verifier looks for, not just that lookup() resolves."""
        from agentit.models import DimensionScore, Finding, Severity
        async_store, raw = store
        report = make_report(scores=[
            DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="rbac", severity=Severity.high,
                        description="No dedicated ServiceAccount", recommendation="Add one"),
            ]),
        ])
        aid = await raw.save(report)
        dispatcher = RemediationDispatcher(async_store)
        result = await dispatcher.dispatch(aid, "rbac")
        assert result["error"] is None
        assert result["agent"] == "security"
        content = "\n".join(f["content"] for f in result["files"])
        assert "ServiceAccount" in content and "RoleBinding" in content

    async def test_dispatch_sbom_generates_task(self, store):
        from agentit.models import DimensionScore, Finding, Severity
        async_store, raw = store
        report = make_report(scores=[
            DimensionScore(dimension="compliance", score=30, max_score=100, findings=[
                Finding(category="sbom supply chain", severity=Severity.medium,
                        description="No SBOM", recommendation="Add SBOM"),
            ]),
        ])
        aid = await raw.save(report)
        dispatcher = RemediationDispatcher(async_store)
        result = await dispatcher.dispatch(aid, "sbom")
        assert result["error"] is None
        assert result["agent"] == "compliance"
        assert any("sbom" in f["path"].lower() for f in result["files"])


# ── Webhook Integration ────────────────────────────────────────────


@pytest.fixture
async def _override_store():
    test_store = await make_store()
    async_store = test_store
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store):
        yield test_store


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True)


class TestFindingWebhook:
    async def test_rejects_missing_category(self, client, _override_store):
        resp = await client.post("/api/webhook/finding", json={"app_name": "test"})
        assert resp.status_code == 400

    async def test_alert_only_for_unknown_app(self, client, _override_store):
        resp = await client.post("/api/webhook/finding", json={
            "app_name": "no-such-app",
            "category": "network",
            "description": "test",
        })
        assert resp.status_code == 200
        assert resp.json()["action"] == "alert-only"

    async def test_generates_fix_but_never_auto_delivers(self, client, _override_store):
        from agentit.models import DimensionScore, Finding, Severity
        store = _override_store
        report = make_report(
            repo_name="gated-app",
            scores=[DimensionScore(dimension="security", score=30, max_score=100, findings=[
                Finding(category="network", severity=Severity.high,
                        description="No NetworkPolicy", recommendation="Add one"),
            ])],
        )
        await store.save(report)
        resp = await client.post("/api/webhook/finding", json={
            "app_name": "gated-app",
            "category": "network",
            "description": "Missing NetworkPolicy",
            "severity": "high",
        })
        assert resp.status_code == 200
        data = resp.json()
        # The `gates` table/generic gate-resolution machinery has been
        # removed entirely (2026-07-19) -- a dispatched fix is generated
        # but never gated/auto-delivered; re-running Onboard is the real
        # next step to review and deliver it from Onboard Results.
        assert data["action"] == "fix-not-delivered"
        assert data["files_generated"] > 0
