from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
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
    parser.addoption(
        "--browser-tests",
        action="store_true",
        default=False,
        help="Run Playwright browser tests (tests marked browser; needs the browser extra + chromium)",
    )


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
    if not config.getoption("--browser-tests"):
        # Full crawl (test_browser.py) stays --ignore'd in CI; the lean
        # critical journeys (test_browser_critical.py) opt in via this flag
        # so capability-scout / default pytest never need Chromium.
        skip = pytest.mark.skip(reason="needs --browser-tests (Playwright + chromium)")
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(skip)
    # No more --run-postgres-tests gate. Postgres is the only supported
    # store (see docs/postgres-migration-plan.md) -- almost the entire
    # suite needs a real instance now, not just a handful of opt-in tests,
    # so requiring an extra flag to run the *default* `pytest` invocation
    # would defeat the point. `postgres_dsn`/`make_store()` below handle
    # getting a real instance transparently (env var, or an
    # auto-started/auto-torn-down throwaway container); a test that
    # genuinely can't reach either still skips itself via that fixture,
    # it's just no longer an opt-in gate on the whole suite.


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


@pytest.fixture(autouse=True)
def _allow_unverified_webhooks_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt into documented local-dev fail-open for webhook auth (ADR 0004).

    Production rejects unsigned webhooks when secrets are unset. Hermetic
    tests that exercise webhook *handlers* (CSRF exemption, finding
    dispatch, onboard flow) use their own ASGI clients without mounting
    secrets — set the escape hatch suite-wide. Fail-closed coverage in
    ``test_webhook_security`` / ``test_credential_health`` clears this
    env in the test body.
    """
    monkeypatch.setenv("AGENTIT_ALLOW_UNVERIFIED_WEBHOOKS", "1")


@pytest.fixture(autouse=True)
def _hermetic_kube_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``AGENTIT_OFFLINE=1`` for every test by default.

    Root cause this closes: unlike ``_hermetic_llm_env`` above (which strips
    ambient LLM credentials so no test can accidentally reach a real LLM),
    nothing previously stopped a test run from reaching a real Kubernetes
    API server. A developer with an expired-but-still-configured ``oc
    login`` session (a live kubeconfig context pointing at a real cluster,
    just with a stale/invalid token) gets a real, slow, failing HTTPS round
    trip out of any code path that calls ``kube.get_client()`` -- not a fast
    local failure. That latency-plus-failure pattern was enough to
    intermittently fail a *different* set of tests on every run (observed:
    two full-suite runs on the exact same uncommitted diff produced two
    disjoint sets of ~11 failures each), because it interacts with
    wall-clock-sensitive shared state (``kube_breaker``'s
    ``reset_after``, background-task timing) in a way that depends on
    exactly how slow the failing real call was and how much other work
    happened to be scheduled around it -- classic environment-dependent
    flakiness, not a code bug in whatever test happened to fail.

    ``kube.py``'s own ``AGENTIT_OFFLINE`` (see ``get_client()``'s
    docstring) already exists as exactly this hard-offline guarantee --
    it was just opt-in, so every dev had to remember to export it by hand
    before running tests locally (the recovery step used once to diagnose
    this very issue). Defaulting it on for the whole suite makes that the
    guarantee rather than a manual step, while still stepping aside
    (leaving any real ``AGENTIT_OFFLINE`` the caller already set alone,
    and never touching it at all) when ``--live-cluster`` is passed, so
    ``live_cluster``-marked tests still see real cluster access when
    explicitly opted in.
    """
    if request.config.getoption("--live-cluster"):
        return
    monkeypatch.setenv("AGENTIT_OFFLINE", "1")


# Successful SSA dry-run shape used when the suite is hermetic-offline.
# ``deliver_with_verification(dry_run=True)`` / auto_delivery call
# ``dry_run_manifests_against_cluster`` → ``kube.apply_yaml``; under
# ``AGENTIT_OFFLINE`` that raises and fail-closes into needs_attention.
# Dedicated unit coverage of the real helper is in test_cluster_dry_run.py
# (opted out below).
_HERMETIC_API_DRY_RUN_OK = {
    "applied": [],
    "skipped": [],
    "errors": [],
    "warnings": [],
    "conflicts": [],
    "missing_operators": {},
    "repo_files": [],
}


@pytest.fixture(autouse=True)
def _hermetic_api_dry_run(request: pytest.FixtureRequest):
    """Mock apiserver SSA dry-run success for the hermetic suite.

    Without this, every auto_validate / Dry Run path fails closed with
    ``AGENTIT_OFFLINE is set`` (see ``_hermetic_kube_env``), cascading
    onboarding jobs into ``needs_attention``. Opt out: ``--live-cluster``,
    or tests in ``test_cluster_dry_run.py`` (they patch ``kube.apply_yaml``
    themselves).
    """
    if request.config.getoption("--live-cluster"):
        yield
        return
    path = getattr(request.node, "path", None) or getattr(request.node, "fspath", None)
    if path is not None and Path(str(path)).name == "test_cluster_dry_run.py":
        yield
        return
    with patch(
        "agentit.portal.cluster_apply.dry_run_manifests_against_cluster",
        return_value=dict(_HERMETIC_API_DRY_RUN_OK),
    ):
        yield


@pytest.fixture(autouse=True)
def _reset_kube_breaker() -> None:
    """Reset the shared, process-global ``kube_breaker`` (portal/helpers.py)
    before and after every test.

    Unlike ``llm_breaker`` (which almost never actually trips in the
    general suite, since ``_hermetic_llm_env`` above keeps
    ``get_llm_client()`` returning ``None`` everywhere outside
    test_llm.py -- so ``LLMClient._chat()``, the only place that touches
    ``llm_breaker``, is rarely even reached), a large fraction of this
    suite deliberately mocks kube.py's real API calls (``core_v1``,
    ``custom_objects``, ``batch_v1``, ``dynamic_client``, ...) to raise,
    specifically to exercise ``KubeError`` handling and other error
    branches. Now that those real API-calling functions feed genuine
    exceptions into ``kube_breaker`` (see kube.py's ``_kube_breaker_scope``),
    those intentionally-mocked failures accumulate against the same
    shared breaker instance across the whole session -- without a
    per-test reset, they can trip it (threshold=5) purely based on
    unrelated test run order, making some later, unrelated test's
    real-call expectations fail because the breaker skipped the call
    instead of reaching the (mocked) client.
    """
    from agentit.portal.helpers import kube_breaker
    kube_breaker._failures = 0
    kube_breaker._last_failure = 0
    yield
    kube_breaker._failures = 0
    kube_breaker._last_failure = 0


# ── Real Postgres test infrastructure ────────────────────────────────
#
# Postgres is the only supported store (docs/postgres-migration-plan.md) --
# there is no more SQLite ":memory:" fast path, so every test that touches
# the store needs a real, reachable Postgres instance. Decision (stated
# explicitly, not silently picked): a *session-scoped* container +
# a *session-scoped* connection pool/AssessmentStore, reused by every test
# that needs one, with per-test isolation via `TRUNCATE ... CASCADE`
# instead of a fresh container/pool per test.
#
# Why not a container (or even just a pool) per test: this suite has ~900+
# tests. `asyncpg.create_pool()` alone is a handful of real TCP
# round-trips: fine once, prohibitively slow multiplied by every single
# test. A fresh *container* per test would be far worse (multi-second
# `podman run` + readiness-poll per test). A single shared container +
# pool for the whole session, cleaned between tests with one cheap
# `TRUNCATE` round-trip, is the standard, correct trade-off here -- it's
# the same "real dependency, cheap per-test reset" shape SQLite's
# ":memory:" gave for free, just achieved differently since there is no
# async in-memory Postgres equivalent.
#
# This requires every async test/fixture in the whole suite to share one
# event loop (an `asyncpg` pool is bound to the loop that created it and
# cannot be driven from a different one) -- see
# `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope`
# in pyproject.toml, both set to "session" for exactly this reason.
_CONTAINER_NAME = "agentit-pg-test"
_container_started = False


def _container_runtime() -> str | None:
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def _wait_for_postgres_dsn(dsn: str, timeout: float = 60.0) -> None:
    """Block until ``dsn``'s host:port accepts TCP (sidecar / CI service race)."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError:
            time.sleep(0.5)


def _resolve_postgres_dsn() -> str | None:
    """Returns a DSN, starting a throwaway container if needed. ``None`` if
    neither a configured DSN nor a container runtime is available -- the
    caller decides whether that's a skip or an error."""
    global _container_started
    env_dsn = os.environ.get("AGENTIT_TEST_PG_DSN")
    if env_dsn:
        # Capability-scout's test-postgres sidecar (and Tekton/GHA services)
        # may still be starting when the first pytest session begins.
        _wait_for_postgres_dsn(env_dsn)
        return env_dsn

    runtime = _container_runtime()
    if runtime is None:
        return None

    port = 55433
    # Always remove by the fixed name so stale containers from previous
    # sessions are cleaned up -- the old random-suffix name meant each
    # session left a stopped container that held a network proxy process,
    # causing "proxy already running" on the next run.
    subprocess.run([runtime, "rm", "-f", _CONTAINER_NAME], capture_output=True)
    # Prune unused networks to release any lingering proxy processes from
    # previously killed containers (common on macOS/podman after a crash).
    subprocess.run([runtime, "network", "prune", "-f"], capture_output=True)
    result = subprocess.run(
        [
            runtime, "run", "-d", "--name", _CONTAINER_NAME,
            "-e", "POSTGRES_USER=agentit_test",
            "-e", "POSTGRES_PASSWORD=agentit_test",
            "-e", "POSTGRES_DB=agentit_test",
            "-p", f"{port}:5432",
            "postgres:16-alpine",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"\nWARNING: could not start Postgres container ({runtime} exited {result.returncode}):\n"
            f"  {result.stderr.strip()}\n"
            f"Set AGENTIT_TEST_PG_DSN to skip auto-start.",
            flush=True,
        )
        return None
    _container_started = True

    dsn = f"postgresql://agentit_test:agentit_test@localhost:{port}/agentit_test"
    for _ in range(30):
        check = subprocess.run(
            [runtime, "exec", _CONTAINER_NAME, "pg_isready", "-U", "agentit_test"],
            capture_output=True,
        )
        if check.returncode == 0:
            return dsn
        time.sleep(1)
    return None


@pytest.fixture(scope="session", autouse=True)
def postgres_dsn():
    """Session-scoped DSN, either from ``AGENTIT_TEST_PG_DSN`` or a
    throw-away container started via podman/docker (torn down at the end
    of the session by ``_pg_session_cleanup`` below).

    Autouse and also exported as ``AGENTIT_DB_DSN`` for the whole test
    session: a handful of code paths (e.g. ``portal/helpers.py::get_store()``'s
    module-level singleton, reached by tests that hit a route without
    patching ``get_store`` at all -- real production behavior, not a test
    gap) read ``AGENTIT_DB_DSN`` straight from the environment rather than
    through a fixture, the same way a real deployment's env would always
    have it set.
    """
    dsn = _resolve_postgres_dsn()
    if dsn is None:
        runtime = _container_runtime()
        if runtime:
            pytest.skip(
                f"no AGENTIT_TEST_PG_DSN and {runtime} failed to start a Postgres container "
                f"(see WARNING above; try: {runtime} machine restart, or set AGENTIT_TEST_PG_DSN)"
            )
        else:
            pytest.skip("no AGENTIT_TEST_PG_DSN and no podman/docker on PATH to start one")
    os.environ["AGENTIT_DB_DSN"] = dsn
    yield dsn


@pytest.fixture(scope="session", autouse=True)
def _pg_session_cleanup():
    """Tears down the throwaway container (if one was started) once, after
    the whole session -- runs unconditionally so it's a no-op when nothing
    needed Postgres, but still cleans up if anything did."""
    yield
    if _container_started:
        runtime = _container_runtime()
        if runtime:
            subprocess.run([runtime, "rm", "-f", _CONTAINER_NAME], capture_output=True)
            subprocess.run([runtime, "network", "prune", "-f"], capture_output=True)


_ALL_STORE_TABLES = (
    "assessments", "apps", "onboarding_results", "events",
    "agent_registry", "slos", "apply_results",
    "settings", "remediation_jobs", "scheduled_operations",
    "processed_webhooks", "agent_feedback", "skill_effectiveness",
    "suppressed_checks", "skill_inventory_snapshots",
    "agent_runs", "check_results", "deliveries", "pr_outcomes",
    "delivery_locks",
)

_shared_store: AssessmentStore | None = None
_shared_store_lock = asyncio.Lock()


async def _get_shared_store() -> AssessmentStore:
    """Session-wide ``AssessmentStore`` singleton, created lazily on first
    use (mirrors ``portal/helpers.py::get_store()``'s own lazy-singleton
    pattern). All async tests share one event loop this session
    (``asyncio_default_test_loop_scope = "session"``), so this is safe to
    reuse across every test without a "different loop" error.
    """
    global _shared_store
    if _shared_store is None:
        async with _shared_store_lock:
            if _shared_store is None:
                dsn = _resolve_postgres_dsn()
                if dsn is None:
                    pytest.skip("no AGENTIT_TEST_PG_DSN and no podman/docker on PATH to start one")
                _shared_store = await AssessmentStore.create(dsn, min_size=2, max_size=10)
    return _shared_store


async def make_store() -> AssessmentStore:
    """The one, real, Postgres-backed ``AssessmentStore`` for this test
    session -- every table truncated first, so each call behaves like the
    "fresh, empty store" the old ``AssessmentStore(db_path=":memory:")``
    gave for free. Every caller is (necessarily) ``async def`` and
    ``await``s this.
    """
    store = await _get_shared_store()
    async with store._pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(_ALL_STORE_TABLES)} CASCADE")
    return store


async def make_async_store():
    """Historically returned ``(async_facade, raw_sync_store)`` -- a
    facade wrapping a synchronous SQLite store plus the raw store itself,
    so tests could make direct synchronous assertions while handing the
    async facade to async-shaped classes (``FleetOrchestrator``/
    ``AutoMode``/etc). There is only one store type now, and it's already
    fully async -- both slots are the exact same object. Kept as a
    2-tuple purely so the ~15 existing call sites that destructure
    ``async_store, raw_store = make_async_store()`` don't all need an
    additional, purely mechanical edit on top of everything else this
    cutover already touches.
    """
    store = await make_store()
    return store, store


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


async def prime_csrf(client) -> None:
    """Fetch the CSRF cookie (set on every response by csrf_middleware) and
    attach it as the X-CSRF-Token header on the client itself, so every
    subsequent request made through this async client automatically
    satisfies the double-submit-cookie check (see csrf.py) -- without having
    to pass a token through each individual `client.post(...)` call.

    This mirrors exactly what base.html's htmx:configRequest handler does in
    a real browser: read the cookie, echo it back as a header.

    ``client`` is an ``httpx.AsyncClient`` (not Starlette's
    ``TestClient``): a sync ``TestClient`` always drives the ASGI app on
    its own separate event-loop thread, which is fundamentally
    incompatible with the real, shared ``asyncpg`` connection pool the
    store fixtures use (an ``asyncpg`` pool is bound to the loop that
    created it) -- ``httpx.AsyncClient`` + ``ASGITransport`` calls the app
    in-process on the *current* running loop instead, so both the test
    body and the app's own route handlers agree on the same loop.
    """
    resp = await client.get("/healthz")
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
async def portal_client():
    """Async HTTP client with all store locations patched and seeded with
    test data.

    ``store`` (the fixture's 2nd yielded value) is the real, async
    ``AssessmentStore`` -- every direct call a test body makes against it
    (e.g. ``await store.log_event(...)``) must now be awaited, since
    there's no more synchronous facade. ``get_store()`` throughout the app
    is patched to return this exact same store instance, so `await
    get_store()` inside the app sees the same data the fixture/test body
    wrote directly.
    """
    from httpx import ASGITransport, AsyncClient
    from agentit.portal.app import app

    store = await make_store()
    report = make_report()
    assessment_id = await store.save(report)
    await store.save_onboarding(assessment_id, [
        {"category": "security", "path": "test.yaml",
         "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test",
         "description": "test file"}
    ])
    await store.log_event("test", "test-action", "test-app", "info", "test event")

    fake_health = {
        "argo_apps": [], "argo_synced": True,
        "pods": [], "pods_running": 0, "pods_failed": 0,
        "pipelines": [], "pipeline_status": "Unknown",
        "kafka_ready": False, "publisher_ok": False,
        "namespace": "agentit", "cluster_url": "local",
        "kafka_stats": {"available": False, "topics": {}, "consumer_groups": []},
    }

    # Portal tests exercise webhook routes without mounting production
    # secrets — opt into the documented local-dev fail-open escape hatch.
    with patch.dict(os.environ, {"AGENTIT_ALLOW_UNVERIFIED_WEBHOOKS": "1"}, clear=False), \
         patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.helpers._store", store), \
         patch("agentit.portal.routes.webhooks.get_store", return_value=store), \
         patch("agentit.portal.routes.health.get_store", return_value=store), \
         patch("agentit.portal.routes.health._get_cluster_health", return_value=fake_health), \
         patch("agentit.portal.routes.schedules.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.recommendations.get_store", return_value=store), \
         patch("agentit.portal.routes.pr_actions.get_store", return_value=store), \
         patch("agentit.portal.routes.capabilities.get_store", return_value=store), \
         patch("agentit.portal.routes.settings.get_store", return_value=store), \
         patch("agentit.portal.routes.insights.get_store", return_value=store), \
         patch("agentit.portal.routes.slos.get_store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store, assessment_id
