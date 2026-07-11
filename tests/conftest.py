from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-real-repos"):
        skip = pytest.mark.skip(reason="needs --run-real-repos flag")
        for item in items:
            if "real_repo" in item.keywords:
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
