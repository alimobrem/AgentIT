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
    parser.addoption("--run-llm-evals", action="store_true", default=False, help="Run tests requiring real LLM credentials")
    parser.addoption("--run-postgres-tests", action="store_true", default=False, help="Run tests requiring a real Postgres instance (podman/docker or AGENTIT_TEST_PG_DSN)")


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
    if not config.getoption("--run-llm-evals"):
        # Gate on the explicit flag, not just credential *presence* — an
        # ambient ANTHROPIC_API_KEY with no working network/quota would
        # otherwise make these run (and fail) instead of skip.
        skip = pytest.mark.skip(reason="needs --run-llm-evals flag (and real LLM credentials)")
        for item in items:
            if "llm_eval" in item.keywords:
                item.add_marker(skip)
    if not config.getoption("--run-postgres-tests"):
        skip = pytest.mark.skip(reason="needs --run-postgres-tests flag (and podman/docker or AGENTIT_TEST_PG_DSN)")
        for item in items:
            if "postgres" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _hermetic_llm_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient LLM credentials from every test's environment by default.

    Root cause of the "test_llm.py flaky under full-suite ordering" issue:
    a dev/CI environment can have real ``ANTHROPIC_API_KEY``/
    ``ANTHROPIC_VERTEX_PROJECT_ID``/``CLOUD_ML_REGION``/
    ``GOOGLE_APPLICATION_CREDENTIALS`` set ambiently (e.g. for an agent
    session that itself runs on Vertex). Several unrelated code paths
    auto-detect LLM availability straight from these env vars --
    ``FleetOrchestrator.run()``'s skill-engine LLM client construction,
    ``portal/helpers.py::get_llm_client()``, ``cli.py``'s
    ``_resolve_and_assess`` -- so any test that exercises one of those
    paths (e.g. any ``FleetOrchestrator(...).run()`` whose report matches
    an LLM-only skill) ends up constructing a *real* ``LLMClient`` and
    attempting a *real* network call. In a sandboxed test process with no
    working credentials, that call fails and increments the shared,
    process-global ``llm_breaker`` circuit breaker
    (``portal/helpers.py::llm_breaker``) via ``LLMClient._chat()``'s
    ``except Exception: llm_breaker.record_failure()`` -- with no matching
    ``record_success()`` to offset it. Three or more such incidental
    failures, scattered harmlessly across otherwise-unrelated test files,
    are enough to trip the breaker (``threshold=3``), which then makes
    ``tests/test_llm.py``'s otherwise-deterministic assertions fail purely
    based on what happened to run before them and how much wall-clock time
    has passed (``reset_after=30s``) -- exactly the "known-flaky under
    full-suite ordering" symptom.

    ``tests/conftest.py``'s own ``pytest_collection_modifyitems`` already
    applies this same "ambient credentials shouldn't make LLM-eval tests
    silently run" philosophy to ``llm_eval``-marked tests specifically;
    this fixture extends it session-wide by default, and steps aside
    (returns immediately) when ``--run-llm-evals`` is passed so those
    tests still see real credentials when explicitly opted in. Any test
    that wants real (fake-but-present) credentials for its own scenario
    can still set them itself via ``monkeypatch.setenv(...)`` in its own
    body -- that happens after this fixture runs and is unaffected by it.
    """
    if request.config.getoption("--run-llm-evals"):
        return
    for var in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)


def make_store() -> AssessmentStore:
    """Create an in-memory assessment store."""
    return AssessmentStore(db_path=":memory:")


def make_async_store():
    """Create an in-memory store plus an async facade over that *same*
    connection (``AsyncSQLiteStore.wrap``, not a second ``:memory:`` store --
    those are separate, isolated databases).

    Returns ``(async_store, raw_store)``. ``async_store`` is what gets handed
    to the now-async ``FleetOrchestrator``/``AutoMode``/``RemediationDispatcher``/
    ``RemediationLoop``; ``raw_store`` is the plain synchronous store for
    direct, non-async test assertions (``raw_store.list_remediations(...)``).
    """
    from agentit.portal.store_factory import AsyncSQLiteStore

    raw_store = make_store()
    return AsyncSQLiteStore.wrap(raw_store), raw_store


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


def prime_csrf(client) -> None:
    """Fetch the CSRF cookie (set on every response by csrf_middleware) and
    attach it as the X-CSRF-Token header on the client itself, so every
    subsequent request made through this TestClient instance automatically
    satisfies the double-submit-cookie check (see csrf.py) -- without having
    to pass a token through each individual `client.post(...)` call.

    This mirrors exactly what base.html's htmx:configRequest handler does in
    a real browser: read the cookie, echo it back as a header.
    """
    resp = client.get("/healthz")
    token = resp.cookies.get("csrf_token") or client.cookies.get("csrf_token")
    if token:
        client.headers["X-CSRF-Token"] = token


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
    """TestClient with all store locations patched and seeded with test data.

    ``store`` (the fixture's 2nd yielded value, and what every test body
    calls directly, e.g. ``store.log_event(...)``) stays the plain
    *synchronous* ``AssessmentStore`` -- unchanged from before Phase 3 of
    docs/postgres-migration-plan.md, so none of test_portal.py's 120 tests
    need to become ``async def`` just to keep making direct store calls.

    What *does* change: the app itself now calls `get_store()` as an
    ``async def`` (Phase 3) -- ``AsyncSQLiteStore.wrap(store)`` gives every
    patched location an async-compatible facade over the exact same
    underlying in-memory sqlite connection (constructing a second, separate
    ``AsyncSQLiteStore(":memory:")`` would silently point at a different,
    empty database), so `await get_store()` inside the app sees the same
    data `store.*` calls made directly in a test body already wrote.
    """
    from fastapi.testclient import TestClient
    from agentit.portal.app import app
    from agentit.portal.store_factory import AsyncSQLiteStore

    store = make_store()
    async_store = AsyncSQLiteStore.wrap(store)
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

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.helpers.get_store", return_value=async_store), \
         patch("agentit.portal.helpers._store", async_store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health.get_store", return_value=async_store), \
         patch("agentit.portal.routes.health._get_cluster_health", return_value=fake_health), \
         patch("agentit.portal.routes.schedules.get_store", return_value=async_store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store), \
         patch("agentit.portal.routes.gates.get_store", return_value=async_store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=async_store), \
         patch("agentit.portal.routes.settings.get_store", return_value=async_store), \
         patch("agentit.portal.routes.insights.get_store", return_value=async_store), \
         patch("agentit.portal.routes.remediations.get_store", return_value=async_store), \
         patch("agentit.portal.routes.slos.get_store", return_value=async_store):
        client = TestClient(app)
        prime_csrf(client)
        yield client, store, assessment_id
