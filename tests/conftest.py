from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)
from agentit.portal.store import AssessmentStore


def pytest_addoption(parser):
    parser.addoption("--run-real-repos", action="store_true", default=False, help="Run tests against real repos")
    parser.addoption("--live-cluster", action="store_true", default=False, help="Run e2e tests against a live OpenShift cluster")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-real-repos"):
        skip = pytest.mark.skip(reason="needs --run-real-repos flag")
        for item in items:
            if "real_repo" in item.keywords:
                item.add_marker(skip)
    if not config.getoption("--live-cluster"):
        skip = pytest.mark.skip(reason="needs --live-cluster flag and active oc login")
        for item in items:
            if "live_cluster" in item.keywords:
                item.add_marker(skip)


def make_store() -> AssessmentStore:
    """Create an in-memory assessment store."""
    return AssessmentStore(db_path=":memory:")


def make_report(
    *,
    repo_name: str = "test-app",
    repo_url: str | None = None,
    languages: list[Language] | None = None,
    scores: list[DimensionScore] | None = None,
    criticality: str = "medium",
    summary: str = "test summary",
) -> AssessmentReport:
    """Create a minimal AssessmentReport for testing."""
    if languages is None:
        languages = [Language(name="python", file_count=10, percentage=100.0)]
    if scores is None:
        scores = [DimensionScore(
            dimension="security", score=80, max_score=100,
            findings=[Finding(category="test", severity=Severity.low,
                              description="minor", recommendation="fix")],
        )]
    return AssessmentReport(
        repo_url=repo_url or f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=languages,
            frameworks=[], databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[],
        ),
        scores=scores,
        criticality=criticality,
        summary=summary,
        remediation_plan=[],
    )


@pytest.fixture
def create_mock_repo(tmp_path: Path):
    """Create a mock repo directory with specified files and contents."""
    def _create(files: dict[str, str]) -> Path:
        repo_dir = tmp_path / "mock_repo"
        repo_dir.mkdir(exist_ok=True)
        for filepath, content in files.items():
            full_path = repo_dir / filepath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
        return repo_dir
    return _create


@pytest.fixture()
def portal_client():
    """TestClient with all store locations patched and seeded with test data."""
    from fastapi.testclient import TestClient
    from agentit.portal.app import app

    store = make_store()
    report = make_report()
    assessment_id = store.save(report)
    store.save_onboarding(assessment_id, [
        {"category": "security", "path": "test.yaml",
         "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
         "description": "test file"}
    ])
    store.log_event("test", "test-action", "test-app", "info", "test event")

    fake_health = {
        "argo_apps": [], "argo_synced": True,
        "pods": [], "pods_running": 0, "pods_failed": 0,
        "pipelines": [], "pipeline_status": "Unknown",
        "kafka_ready": False, "publisher_ok": False,
        "namespace": "agentit", "cluster_url": "local",
        "kafka_stats": {"available": False, "topics": {}, "consumer_groups": []},
    }

    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.helpers._store", store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=store), \
         patch("agentit.portal.routes.health.get_store", return_value=store), \
         patch("agentit.portal.routes.health._get_cluster_health", return_value=fake_health), \
         patch("agentit.portal.routes.schedules.get_store", return_value=store):
        client = TestClient(app)
        yield client, store, assessment_id
