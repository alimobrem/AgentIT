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

    def test_multiword_category_does_not_substring_steal_contract(self):
        """Solution contracts use exact keys only. Substring matching used to
        map ``container security`` → containerfile, but also falsely mapped
        ``cost … resources`` → resource-limits and swallowed chaos/parity
        domains. Multi-word categories stay uncontracted so match() can
        trigger-match them without pinky companions on exact analyzer keys."""
        assert lookup("container security") is None
        assert lookup("container") == ("security", "containerfile")
        assert lookup("cost rightsize resources") is None

    def test_unknown_category_returns_none(self):
        assert lookup("banana") is None

    def test_all_registered_categories(self):
        for cat in ("network", "scanning", "sbom", "pipeline", "metrics", "base_image"):
            result = lookup(cat)
            assert result is not None, f"No fix registered for '{cat}'"
        # tracing is detect-only (app SDK) — collector does not clear
        assert lookup("tracing") is None

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

    def test_source_patch_categories_resolve(self):
        """eol / migration / audit map to source-repo remediation skills."""
        assert lookup("eol") == ("infrastructure", "eol-upgrade")
        assert lookup("migration") == ("data_governance", "db-migration-tooling")
        assert lookup("audit") == ("compliance", "app-audit-logging")
        assert lookup("container") == ("security", "containerfile")

    def test_availability_resolves_to_pdb_not_registry_lookup_alone(self):
        """ha_dr's "No PodDisruptionBudget defined" finding (category
        "availability") had no registry row, so skill_for_category() fell
        back to trigger-keyword matching -- both skills/infrastructure/pdb.md
        (the real remediation) and skills/chaos/pod-delete.md (a resiliency
        -test generator, not a fix) declare trigger "availability", and
        load_all_skills() sorts by path ("skills/chaos/" < "skills/
        infrastructure/" alphabetically), so pod-delete silently won every
        time. This only proves the registry row itself; see
        test_skill_engine.py for the full skill_for_category() proof that
        pod-delete is no longer selected."""
        assert lookup("availability") == ("infrastructure", "pdb")


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

    async def test_dispatch_sbom_generates_ci_workflow(self, store):
        from agentit.models import DimensionScore, Finding, Severity
        async_store, raw = store
        report = make_report(scores=[
            DimensionScore(dimension="compliance", score=30, max_score=100, findings=[
                Finding(category="sbom", severity=Severity.medium,
                        description="No SBOM generation in CI",
                        recommendation="Add CI SBOM generation"),
            ]),
        ])
        aid = await raw.save(report)
        dispatcher = RemediationDispatcher(async_store)
        result = await dispatcher.dispatch(aid, "sbom")
        assert result["error"] is None
        assert result["agent"] == "compliance"
        assert result["method"] == "sbom-ci"
        assert any(
            "workflow" in (f.get("path") or "").lower()
            or "sbom" in (f.get("path") or "").lower()
            for f in result["files"]
        )
        content = "\n".join(f["content"] for f in result["files"])
        assert "anchore/sbom-action" in content


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

    async def test_generates_fix_enters_gated_auto_delivery(self, client, _override_store):
        from unittest.mock import AsyncMock, patch

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
        with patch(
            "agentit.portal.auto_delivery.auto_validate_and_deliver",
            new_callable=AsyncMock,
            return_value={
                "status": "delivered",
                "pr_urls": ["https://github.com/example/infra/pull/1"],
                "reason": "",
            },
        ) as mock_deliver:
            resp = await client.post("/api/webhook/finding", json={
                "app_name": "gated-app",
                "category": "network",
                "description": "Missing NetworkPolicy",
                "severity": "high",
            })
        assert resp.status_code == 200
        data = resp.json()
        # Remediable findings enter gated auto_validate_and_deliver
        # (finding_gate + clear-evidence). Human gate = merge only.
        assert data["action"] == "delivered"
        assert data["files_generated"] > 0
        assert data["pr_urls"]
        mock_deliver.assert_awaited_once()
        kwargs = mock_deliver.await_args.kwargs
        assert kwargs["actor"] == "webhook-finding"
        assert any(c == "network" for c, _ in kwargs["target_findings"])
