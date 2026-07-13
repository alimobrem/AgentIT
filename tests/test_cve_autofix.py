from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

import agentit.agents.hardening as _hardening_module
from agentit.agents.cicd import CICDAgent
from agentit.agents.hardening import HardeningAgent
from agentit.analyzers.security import SecurityAnalyzer
from agentit.models import DimensionScore, Finding, Language, Severity
from agentit.portal.app import app, get_store
from conftest import make_report, make_store

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_has_patch_base_image = hasattr(_hardening_module, "patch_base_image")


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
        return _hardening_module.patch_base_image(dockerfile, language)  # type: ignore[attr-defined]

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


# ── TestScanTaskNotifyStep ───────────────────────────────────────────


class TestScanTaskNotifyStep:
    def test_scan_task_has_notify_step(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[
                _score_with_finding(
                    "security", "scanning", "No vulnerability scanning detected in CI",
                ),
            ],
        )
        result = HardeningAgent(report, tmp_path / "out").run()

        scan_files = [f for f in result.files if f.path == "image-scan-task.yaml"]
        assert len(scan_files) == 1, (
            f"Expected image-scan-task.yaml, got: {[f.path for f in result.files]}"
        )

        task = yaml.safe_load(scan_files[0].content)
        steps = task["spec"]["steps"]
        assert len(steps) == 2, (
            f"Expected 2 steps (scan + notify-cve), got {len(steps)}: "
            f"{[s['name'] for s in steps]}"
        )
        assert steps[1]["name"] == "notify-cve"
        assert "webhook/finding" in steps[1]["script"]


# ── TestCVEWebhook ───────────────────────────────────────────────────


class TestCVEWebhook:
    @pytest.fixture(autouse=True)
    def _override_store(self):
        test_store = make_store()
        with patch("agentit.portal.app.get_store", return_value=test_store), \
             patch("agentit.portal.routes.webhooks.get_store", return_value=test_store), \
             patch("agentit.portal.routes.health.get_store", return_value=test_store), \
             patch("agentit.portal.routes.schedules.get_store", return_value=test_store):
            yield test_store

    @pytest.fixture()
    def client(self):
        return TestClient(app)

    def test_finding_webhook_logs_event(self, client: TestClient, _override_store) -> None:
        store = _override_store
        resp = client.post(
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
        events = store.list_events(limit=50)
        finding_events = [e for e in events if e["action"] == "finding-received"]
        assert len(finding_events) >= 1, (
            f"Expected a finding-received event, got actions: {[e['action'] for e in events]}"
        )

    def test_finding_webhook_returns_alert_only_for_unknown_app(
        self, client: TestClient, _override_store,
    ) -> None:
        resp = client.post(
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

    def test_finding_webhook_auto_mode_decision_attributed_to_real_agent(
        self, client: TestClient, _override_store,
    ) -> None:
        """webhook_finding already knows which agent (dispatcher's result["agent"])
        generated the fix — the auto-mode decision it triggers should be logged
        under that real agent name, not the generic 'auto-mode' component name."""
        store = _override_store
        report = make_report(
            repo_name="netpol-app",
            scores=[_score_with_finding("security", "network", "Missing NetworkPolicy")],
        )
        store.save(report)
        store.set_setting("auto_mode", "true")

        fake_llm = type("FakeLLM", (), {
            "classify_action": staticmethod(lambda **kw: {
                "is_destructive": False, "confidence": 0.95, "reason": "Adds NetworkPolicy",
            }),
        })()

        with patch("agentit.portal.routes.webhooks.get_llm_client", return_value=fake_llm):
            resp = client.post(
                "/api/webhook/finding",
                json={
                    "app_name": "netpol-app",
                    "category": "network",
                    "description": "Missing NetworkPolicy",
                    "severity": "high",
                },
            )
        assert resp.status_code == 200

        decision_events = store.list_events_by_action("decision")
        assert len(decision_events) == 1
        assert decision_events[0]["agent_id"] == "hardening"


# ── TestFullCVELoop ──────────────────────────────────────────────────


@pytest.mark.live_cluster
class TestFullCVELoop:
    def test_vulnerable_image_detected_and_fixed(self, tmp_path: Path) -> None:
        # 1. Run assessment on sample-app (has non-UBI base image)
        analyzer = SecurityAnalyzer()
        score = analyzer.analyze(FIXTURES_DIR / "sample-app")
        non_ubi = [
            f for f in score.findings
            if f.category == "container" and "not UBI" in f.description
        ]
        assert len(non_ubi) >= 1, "Assessment should detect non-UBI base image"

        # 2. Run hardening agent with scanning finding to get image-scan-task
        report = make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            scores=[
                DimensionScore(
                    dimension="security",
                    score=30,
                    max_score=100,
                    findings=[
                        Finding(
                            category="container",
                            severity=Severity.high,
                            description="No Dockerfile found",
                            recommendation="Add Containerfile",
                        ),
                        Finding(
                            category="scanning",
                            severity=Severity.high,
                            description="No vulnerability scanning in CI",
                            recommendation="Add scanning",
                        ),
                    ],
                ),
            ],
        )
        hardening_out = tmp_path / "hardening"
        hardening_result = HardeningAgent(report, hardening_out).run()

        # Verify Containerfile uses UBI
        containerfiles = [f for f in hardening_result.files if f.path == "Containerfile"]
        assert len(containerfiles) == 1
        assert "ubi9" in containerfiles[0].content

        # Verify image-scan-task.yaml has notify step
        scan_tasks = [
            f for f in hardening_result.files if f.path == "image-scan-task.yaml"
        ]
        assert len(scan_tasks) == 1
        task_doc = yaml.safe_load(scan_tasks[0].content)
        assert any(
            s["name"] == "notify-cve" for s in task_doc["spec"]["steps"]
        ), "image-scan-task.yaml should have notify-cve step"

        # 3. Run CI/CD agent -- pipeline should include image-scan step
        cicd_report = make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            scores=[
                _score_with_finding(
                    "cicd", "pipeline cicd", "No CI/CD pipeline",
                ),
            ],
        )
        cicd_out = tmp_path / "cicd"
        cicd_result = CICDAgent(cicd_report, cicd_out).run()
        pipeline_files = [
            f for f in cicd_result.files if f.path == "tekton-pipeline.yaml"
        ]
        assert len(pipeline_files) >= 1
        pipeline_docs = list(yaml.safe_load_all(pipeline_files[0].content))
        pipeline_doc = next(
            (d for d in pipeline_docs if d.get("kind") == "Pipeline"), None,
        )
        assert pipeline_doc is not None
        task_names = [
            t["name"] for t in pipeline_doc["spec"]["tasks"]
        ]
        assert "image-scan" in task_names, (
            f"Pipeline should have image-scan task, got: {task_names}"
        )
        # deploy must run after image-scan (blocks deploy)
        deploy_task = next(
            t for t in pipeline_doc["spec"]["tasks"] if t["name"] == "deploy"
        )
        assert "image-scan" in deploy_task.get("runAfter", [])

        # 4. patch_base_image on the original Dockerfile
        if _has_patch_base_image:
            original_df = (FIXTURES_DIR / "sample-app" / "Dockerfile").read_text()
            patched = _hardening_module.patch_base_image(original_df, "python")  # type: ignore[attr-defined]
            assert patched is not None
            assert "ubi9" in patched
        else:
            pytest.skip("patch_base_image not yet implemented")
