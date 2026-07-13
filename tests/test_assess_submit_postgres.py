"""Proof that `POST /assess`'s background-thread assessment pipeline
(`assess_submit`'s `_run()`) genuinely supports the Postgres-backed store --
the exact live bug fixed in this pass: `store_pg.AssessmentStore` has no
`.raw`, so `assess_submit` used to fail every submission with a fail-loud
500 (see docs/postgres-migration-plan.md and the commit that fixed this)
the instant `AGENTIT_DB_BACKEND=postgres`.

Calls the route coroutine directly (bypassing FastAPI's routing/Form
machinery, `request` is unused by `assess_submit`'s body) so the whole test
runs on *one* event loop throughout -- the same loop that constructs the
`asyncpg`-backed store via the `pg_store` fixture, and the same loop
`assess_submit` captures via `asyncio.get_running_loop()` before spawning
its background thread. This mirrors the portal's real deployment (one
persistent uvicorn event loop for the whole process) far more faithfully
than a `TestClient` not used as a context manager, which tears its own
event loop down after every individual request and therefore can't
exercise this specific bridge -- see assess_submit's own comments.

Requires a real Postgres instance; gated the same way as
tests/test_store_pg.py (`--run-postgres-tests`, reusing its `postgres_dsn`
fixture).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentit.models import (
    ArchitectureInfo, AssessmentReport, DimensionScore, Finding,
    Language, Severity, StackInfo,
)
from agentit.portal import store_pg
from agentit.portal.routes import assessments
from test_store_pg import postgres_dsn  # noqa: F401 -- reused fixture, see module docstring

pytestmark = pytest.mark.postgres


def _make_report(repo_name: str = "pg-assess-app") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(languages=[Language(name="python", file_count=5, percentage=100.0)],
                         frameworks=[], databases=[], runtimes=[], package_managers=[]),
        architecture=ArchitectureInfo(service_count=1, architecture_style="monolith",
                                       has_api=True, api_style="REST", external_dependencies=[]),
        scores=[DimensionScore(dimension="security", score=70, max_score=100,
                                findings=[Finding(category="test", severity=Severity.low,
                                                   description="d", recommendation="r")])],
        criticality="medium", summary="s", remediation_plan=[],
    )


@pytest.fixture
async def pg_store(postgres_dsn):
    """Real, independent `store_pg.AssessmentStore` built on *this test's*
    running event loop -- the same loop `assess_submit` will capture."""
    store = await store_pg.AssessmentStore.create(postgres_dsn, min_size=1, max_size=5)
    async with store._pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE assessments, onboarding_results, events, gates, remediations, "
            "agent_registry, slos, apply_results, settings, remediation_jobs, "
            "scheduled_operations, processed_webhooks, agent_feedback, "
            "skill_effectiveness, suppressed_checks, skill_inventory_snapshots, "
            "agent_runs, check_results CASCADE"
        )
    yield store
    await store.close()


async def test_assess_submit_completes_against_real_postgres(pg_store):
    """`assess_submit` must not 500 under the Postgres backend, and its
    background thread must actually run the clone+assess pipeline to
    completion and persist a real, queryable assessment -- not just "the
    coroutine didn't raise"."""
    report = _make_report()

    # The background thread `assess_submit` spawns keeps running well after
    # the coroutine itself returns (that's the whole point of the fire-and-
    # forget pattern under test) -- these patches must stay active for the
    # thread's entire lifetime, not just for the `await` below, so the
    # polling loop that waits for it to finish has to live inside this
    # `with` block too.
    with patch.object(assessments, "get_store", return_value=pg_store), \
         patch.object(assessments, "clone_repo", return_value=Path("/tmp/fake-pg-assess-repo")), \
         patch.object(assessments, "run_assessment", return_value=report), \
         patch.object(assessments, "_auto_create_infra_repo", return_value=None):
        response = await assessments.assess_submit(
            request=None, repo_url=report.repo_url, criticality="medium", infra_repo_url="",
        )

        assert response.status_code == 303
        job_id = response.headers["location"].split("/assess/progress/")[1]

        deadline = asyncio.get_running_loop().time() + 15.0
        job = None
        while asyncio.get_running_loop().time() < deadline:
            job = await pg_store.get_remediation_job(job_id)
            assert job is not None, f"job {job_id} vanished"
            if job["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.2)

    assert job is not None and job["status"] == "completed", (
        f"assess job did not complete against Postgres in time: {job}"
    )
    assessment_id = job["assessment_id"]
    assert assessment_id, "completed job has no assessment_id"

    # Independent read-back through the same store, proving the assessment
    # (and its check results / history) genuinely landed in Postgres, not
    # just that the background thread ran without raising.
    saved = await pg_store.get(assessment_id)
    assert saved is not None
    assert saved.repo_url == report.repo_url

    history = await pg_store.list_history(report.repo_url)
    assert len(history) == 1  # proves save_check_results/list_history (both bridged calls) ran without raising
