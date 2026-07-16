from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import agentit.remediation.base_image as _base_image_module
from agentit.analyzers.security import SecurityAnalyzer
from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.app import app, get_store
from conftest import make_report, make_store

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_has_patch_base_image = hasattr(_base_image_module, "patch_base_image")


def _score_with_finding(dimension: str, category: str, desc: str) -> DimensionScore:
    return DimensionScore(
        dimension=dimension,
        score=30,
        max_score=100,
        findings=[
            Finding(
                category=category,
                severity=Severity.high,
                description=desc,
                recommendation="fix it",
            ),
        ],
    )


# ── TestBaseImageDetection ───────────────────────────────────────────


class TestBaseImageDetection:
    def test_detects_vulnerable_base_image(self) -> None:
        analyzer = SecurityAnalyzer()
        score = analyzer.analyze(FIXTURES_DIR / "sample-app")
        container_findings = [
            f for f in score.findings
            if f.category == "container" and "not UBI" in f.description
        ]
        assert len(container_findings) >= 1, (
            f"Expected a non-UBI base image finding, got: {[f.description for f in score.findings]}"
        )

    def test_detects_ubi_base_as_safe(self) -> None:
        analyzer = SecurityAnalyzer()
        score = analyzer.analyze(FIXTURES_DIR / "sample-app-secure")
        non_ubi_findings = [
            f for f in score.findings
            if f.category == "container" and "not UBI" in f.description
        ]
        assert len(non_ubi_findings) == 0, (
            f"UBI base image should not trigger non-UBI finding, got: "
            f"{[f.description for f in non_ubi_findings]}"
        )


# ── TestBaseImagePatch ───────────────────────────────────────────────


@pytest.mark.skipif(
    not _has_patch_base_image,
    reason="patch_base_image not yet implemented in hardening module",
)
class TestBaseImagePatch:
    def _patch(self, dockerfile: str, language: str) -> str | None:
        return _base_image_module.patch_base_image(dockerfile, language)

    def test_patches_python_base_to_ubi(self) -> None:
        dockerfile = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nUSER 1001\n"
        result = self._patch(dockerfile, "python")
        assert result is not None
        assert result.startswith("FROM registry.access.redhat.com/ubi9/python-312:latest")
        assert "WORKDIR /app" in result
        assert "USER 1001" in result

    def test_patches_node_base_to_ubi(self) -> None:
        dockerfile = "FROM node:20-slim\nWORKDIR /app\n"
        result = self._patch(dockerfile, "javascript")
        assert result is not None
        assert "ubi9/nodejs-20" in result

    def test_patches_go_base_to_ubi(self) -> None:
        dockerfile = "FROM golang:1.22\nWORKDIR /app\n"
        result = self._patch(dockerfile, "go")
        assert result is not None
        assert "ubi9/ubi-minimal" in result

    def test_patches_java_base_to_ubi(self) -> None:
        dockerfile = "FROM openjdk:21\nWORKDIR /app\n"
        result = self._patch(dockerfile, "java")
        assert result is not None
        assert "ubi9/openjdk-21" in result

    def test_preserves_existing_ubi_base(self) -> None:
        dockerfile = (
            "FROM registry.access.redhat.com/ubi9/python-312:latest\n"
            "WORKDIR /app\n"
        )
        result = self._patch(dockerfile, "python")
        assert result is None

    def test_preserves_multi_stage_build(self) -> None:
        dockerfile = (
            "FROM golang:1.22 AS builder\n"
            "RUN go build\n"
            "FROM python:3.12\n"
            "COPY --from=builder /app /app\n"
        )
        result = self._patch(dockerfile, "python")
        assert result is not None
        # Build stage should be untouched
        assert "FROM golang:1.22 AS builder" in result
        # Final stage should use UBI
        assert "ubi9/python-312" in result


# ── TestCVEWebhook ───────────────────────────────────────────────────


class TestCVEWebhook:
    @pytest.fixture(autouse=True)
    async def _override_store(self):
        test_store = await make_store()
        async_store = test_store
        with patch("agentit.portal.app.get_store", return_value=async_store), \
             patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
             patch("agentit.portal.routes.health.get_store", return_value=async_store), \
             patch("agentit.portal.routes.schedules.get_store", return_value=async_store):
            yield test_store

    @pytest.fixture()
    def client(self):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True)

    async def test_finding_webhook_logs_event(self, client, _override_store) -> None:
        store = _override_store
        resp = await client.post(
            "/api/webhook/finding",
            json={
                "app_name": "test",
                "category": "base_image",
                "description": "5 CVEs in python:3.12-slim",
                "severity": "critical",
                "source": "trivy",
            },
        )
        assert resp.status_code == 200
        events = await store.list_events(limit=50)
        finding_events = [e for e in events if e["action"] == "finding-received"]
        assert len(finding_events) >= 1, (
            f"Expected a finding-received event, got actions: {[e['action'] for e in events]}"
        )

    async def test_finding_webhook_returns_alert_only_for_unknown_app(
        self, client, _override_store,
    ) -> None:
        resp = await client.post(
            "/api/webhook/finding",
            json={
                "app_name": "nonexistent",
                "category": "network",
                "description": "Missing NetworkPolicy",
                "severity": "high",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "alert-only"

    async def test_finding_webhook_auto_mode_decision_attributed_to_real_agent(
        self, client, _override_store,
    ) -> None:
        """webhook_finding already knows which agent (dispatcher's result["agent"])
        generated the fix — the auto-mode decision it triggers should be logged
        under that real agent name, not the generic 'auto-mode' component name."""
        store = _override_store
        report = make_report(
            repo_name="netpol-app",
            scores=[_score_with_finding("security", "network", "Missing NetworkPolicy")],
        )
        await store.save(report)
        await store.set_setting("auto_mode", "true")

        fake_llm = type("FakeLLM", (), {
            "classify_action": staticmethod(lambda **kw: {
                "is_destructive": False, "confidence": 0.95, "reason": "Adds NetworkPolicy",
            }),
        })()

        with patch("agentit.portal.routes.webhooks.get_llm_client", return_value=fake_llm):
            resp = await client.post(
                "/api/webhook/finding",
                json={
                    "app_name": "netpol-app",
                    "category": "network",
                    "description": "Missing NetworkPolicy",
                    "severity": "high",
                },
            )
        assert resp.status_code == 200

        decision_events = await store.list_events_by_action("decision")
        assert len(decision_events) == 1
        # HardeningAgent was removed once skills covered its domain (see
        # docs/agent-removal-readiness.md) -- the dispatcher now attributes
        # by skill domain ("security") instead of the old agent class name.
        assert decision_events[0]["agent_id"] == "security"
