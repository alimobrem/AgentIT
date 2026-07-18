"""Integration tests for ``AssessmentStore`` (``portal/store.py``) --
the one and only supported store. Postgres is not a backend option among
several; it is the store, so these tests run unconditionally (no
opt-in flag) using the real, session-shared Postgres instance
``tests/conftest.py`` provides (auto-started via podman/docker if
``AGENTIT_TEST_PG_DSN`` isn't set) -- see that module's docstring for the
full session-scoped-container-and-pool rationale.

Formerly ``test_store_pg.py``, from when ``store_pg.py`` was a separate,
not-yet-wired-in async counterpart to a synchronous SQLite ``store.py`` --
see docs/postgres-migration-plan.md for that history.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
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
from agentit.portal.store import AssessmentStore, create_store
from conftest import make_store


def _make_report(repo_name: str = "test-app") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[],
        ),
        scores=[DimensionScore(
            dimension="security", score=80, max_score=100,
            findings=[Finding(category="test", severity=Severity.critical,
                               description="minor", recommendation="fix")],
        )],
        criticality="medium",
        summary="test summary",
        remediation_plan=[],
    )


@pytest.fixture
async def store():
    """The session-shared ``AssessmentStore`` from ``tests/conftest.py``,
    with every table truncated first for isolation -- see that module's
    docstring for why this is a shared session-scoped pool rather than a
    fresh one per test."""
    return await make_store()


class TestCreate:
    async def test_create_returns_assessment_store(self, postgres_dsn):
        s = await AssessmentStore.create(postgres_dsn, min_size=1, max_size=2)
        try:
            assert isinstance(s, AssessmentStore)
        finally:
            await s.close()

    async def test_create_raises_without_dsn(self, monkeypatch):
        monkeypatch.delenv("AGENTIT_DB_DSN", raising=False)
        with pytest.raises(ValueError):
            await AssessmentStore.create(dsn=None)

    async def test_create_store_function_delegates_to_assessment_store_create(self, postgres_dsn):
        """``create_store()`` (used by every real caller -- cli.py, the
        watchers, portal/helpers.py) is a thin wrapper -- verify it
        genuinely delegates rather than duplicating construction logic."""
        s = await create_store(postgres_dsn, min_size=1, max_size=2)
        try:
            assert isinstance(s, AssessmentStore)
        finally:
            await s.close()


class TestSettings:
    async def test_set_and_get_setting(self, store):
        await store.set_setting("theme", "dark")
        assert await store.get_setting("theme") == "dark"

    async def test_set_setting_overwrites(self, store):
        await store.set_setting("theme", "dark")
        await store.set_setting("theme", "light")
        assert await store.get_setting("theme") == "light"

    async def test_get_missing_setting(self, store):
        assert await store.get_setting("nope") is None

    async def test_list_settings(self, store):
        await store.set_setting("a", "1")
        await store.set_setting("b", "2")
        rows = await store.list_settings()
        assert [r["key"] for r in rows] == ["a", "b"]
        # Shape parity check: updated_at comes back as an ISO string, not a
        # datetime object (see module docstring in store_pg.py).
        assert isinstance(rows[0]["updated_at"], str)


class TestAssessments:
    async def test_save_and_get_roundtrip(self, store):
        report = _make_report()
        assessment_id = await store.save(report)
        fetched = await store.get(assessment_id)
        assert fetched is not None
        assert fetched.repo_name == "test-app"
        assert fetched.overall_score == report.overall_score

    async def test_get_missing_returns_none(self, store):
        assert await store.get(uuid.uuid4().hex) is None

    async def test_save_logs_event(self, store):
        assessment_id = await store.save(_make_report())
        events = await store.list_events()
        assert any(e["action"] == "assessment-complete" for e in events)

    async def test_list_all_orders_desc(self, store):
        await store.save(_make_report("older"))
        await store.save(_make_report("newer"))
        rows = await store.list_all()
        assert len(rows) == 2
        assert isinstance(rows[0]["assessed_at"], str)

    async def test_save_carries_forward_infra_repo_url_from_prior_assessment(self, store):
        """Postgres counterpart of the sqlite ``store.py`` regression test --
        re-assessing an already-GitOps-registered app must not lose
        ``infra_repo_url`` just because the new report didn't set it."""
        aid1 = await store.save(_make_report("pinky"))
        await store.set_infra_repo_url(aid1, "https://github.com/org/infra")

        aid2 = await store.save(_make_report("pinky"))
        fetched = await store.get(aid2)
        assert fetched.infra_repo_url == "https://github.com/org/infra"

    async def test_save_does_not_override_explicit_infra_repo_url(self, store):
        aid1 = await store.save(_make_report("pinky"))
        await store.set_infra_repo_url(aid1, "https://github.com/org/old-infra")

        report2 = _make_report("pinky")
        report2.infra_repo_url = "https://github.com/org/new-infra"
        aid2 = await store.save(report2)

        fetched = await store.get(aid2)
        assert fetched.infra_repo_url == "https://github.com/org/new-infra"

    async def test_delete_cascades(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_onboarding(assessment_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])
        assert await store.delete(assessment_id) is True
        assert await store.get(assessment_id) is None
        assert await store.get_onboarding(assessment_id) is None

    async def test_delete_missing_returns_false(self, store):
        assert await store.delete(uuid.uuid4().hex) is False

    async def test_delete_removes_every_historical_assessment_for_the_app(self, store):
        """A Delete on the app's LATEST assessment must remove every prior
        assessment of that same repo_url too (plus their gates/
        remediations/slos/onboarding), not just the one id passed in --
        otherwise get_fleet_data()'s MAX(assessed_at) join resurrects the
        "deleted" app via an older surviving assessment row, contradicting
        fleet.html's "cannot be undone" confirm text."""
        old_id = await store.save(_make_report("multi-run-app"))
        new_id = await store.save(_make_report("multi-run-app"))

        gate_id = await store.create_gate(old_id, "rollback-review", "old gate")
        rem_id = await store.save_remediation(old_id, "security", "old remediation")
        slo_id = await store.save_slo(old_id, "latency_p99_ms", 200)
        await store.save_onboarding(old_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])

        # Sanity: the app shows up via its latest assessment before delete.
        fleet = await store.get_fleet_data()
        assert any(r["id"] == new_id for r in fleet)

        assert await store.delete(new_id) is True

        # The app must not reappear via the older, surviving assessment.
        fleet_after = await store.get_fleet_data()
        assert not any(r["repo_url"] == "https://github.com/org/multi-run-app" for r in fleet_after)

        # Every assessment row for this repo_url is gone, including the old one.
        assert await store.get(old_id) is None
        assert await store.get(new_id) is None

        # Every dependent of the OLD assessment (not just the latest) is gone too.
        assert await store.list_gates_for_assessment(old_id) == []
        assert await store.list_remediations(old_id) == []
        assert await store.list_slos(old_id) == []
        assert await store.get_onboarding(old_id) is None
        gate_row = await store._pool.fetchrow("SELECT 1 FROM gates WHERE id = $1", gate_id)
        assert gate_row is None
        rem_row = await store._pool.fetchrow("SELECT 1 FROM remediations WHERE id = $1", rem_id)
        assert rem_row is None
        slo_row = await store._pool.fetchrow("SELECT 1 FROM slos WHERE id = $1", slo_id)
        assert slo_row is None


class TestFleetAndTrend:
    async def test_get_fleet_data(self, store):
        await store.save(_make_report("app-a"))
        fleet = await store.get_fleet_data()
        assert len(fleet) == 1
        assert fleet[0]["repo_name"] == "app-a"
        assert fleet[0]["critical_count"] == 1
        assert isinstance(fleet[0]["last_assessed"], str)
        assert fleet[0]["ever_onboarded"] is False

    async def test_fleet_ever_onboarded_survives_reassessment(self, store):
        """Onboarding on an older assessment still marks the fleet row."""
        old_id = await store.save(_make_report("refresh-app"))
        await store.save_onboarding(old_id, [{
            "category": "x", "path": "a.yaml", "content": "", "description": "",
        }])
        new_id = await store.save(_make_report("refresh-app"))
        assert new_id != old_id
        fleet = [r for r in await store.get_fleet_data() if r["repo_name"] == "refresh-app"]
        assert fleet[0]["ever_onboarded"] is True
        assert await store.repo_has_onboarding(fleet[0]["repo_url"]) is True

    async def test_fleet_collapses_git_suffix_and_trailing_slash_duplicates(self, store):
        """Regression: the same repo submitted once without a `.git` suffix
        (e.g. via `self_assess_route`'s hardcoded URL) and once with one
        (e.g. pasted from GitHub's own "Clone" HTTPS URL, or via a
        manually-typed "Assess New Repo" submission) must land in
        `get_fleet_data()` as ONE row, not two -- `repo_name` already
        collapses these superficially (both display as the same name),
        which is exactly what made two real rows look like a grouping bug
        rather than the two-distinct-repo_url-strings issue it actually is.
        """
        from conftest import make_report

        bare_id = await store.save(make_report(repo_name="dup-app", repo_url="https://github.com/org/dup-app"))
        dotgit_id = await store.save(make_report(repo_name="dup-app", repo_url="https://github.com/org/dup-app.git"))
        slash_id = await store.save(make_report(repo_name="dup-app", repo_url="https://github.com/org/dup-app/"))

        fleet = [r for r in await store.get_fleet_data() if r["repo_name"] == "dup-app"]
        assert len(fleet) == 1
        assert fleet[0]["repo_url"] == "https://github.com/org/dup-app"
        assert fleet[0]["assessment_count"] == 3

        # The stored reports themselves were normalized too, not just the
        # fleet aggregation query -- `list_history`/`repo_has_onboarding`/etc
        # all key on the exact stored string, so this must be true for
        # those to see one identity rather than three.
        for aid in (bare_id, dotgit_id, slash_id):
            report = await store.get(aid)
            assert report.repo_url == "https://github.com/org/dup-app"

    async def test_normalize_repo_url_preserves_case(self):
        """Deliberately NOT case-folded -- see `normalize_repo_url()`'s
        docstring for why."""
        from agentit.portal.store import normalize_repo_url
        assert normalize_repo_url("https://github.com/AliMobrem/AgentIT.git/") == \
            "https://github.com/AliMobrem/AgentIT"

    async def test_assessment_job_continue_onboard_claim_once(self, store):
        job_id = await store.create_assessment_job(
            "https://github.com/org/chain-app", continue_onboard=True,
        )
        job = await store.get_remediation_job(job_id)
        assert "continue_onboard" in job["steps_completed"]
        assert await store.claim_continue_onboard(job_id) is True
        assert await store.claim_continue_onboard(job_id) is False
        job = await store.get_remediation_job(job_id)
        assert "continue_onboard" not in job["steps_completed"]

    async def test_get_trend_no_history(self, store):
        trend = await store.get_trend("https://github.com/org/nope")
        assert trend == {
            "current_score": None, "previous_score": None,
            "delta": None, "assessments_count": 0,
        }


class TestRepoUrlNormalizationDbTrigger:
    """DB-layer backstop for the whole "duplicate Fleet row" bug class:
    ``SCHEMA_SQL``'s ``normalize_repo_url_before_write`` trigger mirrors
    ``normalize_repo_url()`` in SQL and fires on every INSERT/UPDATE of
    ``repo_url`` on ``assessments``/``apps`` -- so a non-canonical value
    (a `.git` suffix, a trailing slash) can't reach storage even from a
    write path that bypasses ``store.py`` entirely (a raw SQL statement, a
    future code path, a one-off migration script), not just ones that
    remember to call the Python ``normalize_repo_url()`` first. These
    tests deliberately go around ``AssessmentStore.save()``/``_upsert_app()``
    -- straight to ``store._pool`` -- to prove the guarantee holds at the
    database layer itself, independent of any application code.
    """

    async def test_insert_into_assessments_is_normalized_at_the_db_layer(self, store):
        await store._pool.execute(
            """
            INSERT INTO assessments (id, repo_url, repo_name, assessed_at, criticality, overall_score, report_json)
            VALUES ($1, $2, $3, now(), 'medium', 80, '{}'::jsonb)
            """,
            uuid.uuid4().hex, "https://github.com/org/raw-insert-app.git/", "raw-insert-app",
        )
        row = await store._pool.fetchrow(
            "SELECT repo_url FROM assessments WHERE repo_name = 'raw-insert-app'"
        )
        assert row["repo_url"] == "https://github.com/org/raw-insert-app"

    async def test_insert_into_apps_is_normalized_at_the_db_layer(self, store):
        now = datetime.now(timezone.utc)
        await store._pool.execute(
            "INSERT INTO apps (repo_url, repo_name, created_at, updated_at) VALUES ($1, $2, $3, $3)",
            "https://github.com/org/raw-apps-app/", "raw-apps-app", now,
        )
        row = await store._pool.fetchrow(
            "SELECT repo_url FROM apps WHERE repo_name = 'raw-apps-app'"
        )
        assert row["repo_url"] == "https://github.com/org/raw-apps-app"

    async def test_update_of_repo_url_is_also_normalized(self, store):
        aid = await store.save(_make_report("update-norm-app"))
        await store._pool.execute(
            "UPDATE assessments SET repo_url = $1 WHERE id = $2",
            "https://github.com/org/UPDATE-NORM-APP.GIT", aid,
        )
        row = await store._pool.fetchrow("SELECT repo_url FROM assessments WHERE id = $1", aid)
        assert row["repo_url"] == "https://github.com/org/UPDATE-NORM-APP"

    async def test_apps_primary_key_rejects_a_dotgit_variant_of_an_existing_row(self, store):
        """The strongest guarantee this trigger buys: ``apps.repo_url``'s
        existing PRIMARY KEY now enforces uniqueness on the NORMALIZED
        identity, not just the raw string -- a second raw INSERT spelling
        an already-registered repo with a `.git` suffix is rejected
        outright by Postgres, not silently accepted as a second row."""
        now = datetime.now(timezone.utc)
        await store._pool.execute(
            "INSERT INTO apps (repo_url, repo_name, created_at, updated_at) VALUES ($1, $2, $3, $3)",
            "https://github.com/org/pk-collision-app", "pk-collision-app", now,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await store._pool.execute(
                "INSERT INTO apps (repo_url, repo_name, created_at, updated_at) VALUES ($1, $2, $3, $3)",
                "https://github.com/org/pk-collision-app.git", "pk-collision-app", now,
            )


class TestDedupeRepoUrls:
    """``AssessmentStore.dedupe_repo_urls()`` -- the self-healing
    complement to the DB trigger above. That trigger only stops *future*
    writes; it can't retroactively fix rows written before it existed --
    exactly the real incident this backs up: a Tekton
    ``register-self-in-fleet`` step posted a hardcoded `.git`-suffixed
    ``repo-url`` before ``normalize_repo_url()`` was live, briefly
    creating a second Fleet row for AgentIT itself that a human had to
    clean up by hand via a live DB query / ``AssessmentStore.delete()``.
    This is the "no one available to do that by hand" fallback.
    """

    async def _insert_legacy_unnormalized_row(
        self, store, *, repo_url: str, repo_name: str, assessed_at: datetime | None = None,
    ) -> str:
        """Simulates a row written before ``normalize_repo_url_before_write``
        existed, by briefly disabling it so a raw, non-canonical
        ``repo_url`` actually lands as-is -- the same way real rows did in
        the field before the normalization trigger (and before
        ``normalize_repo_url()`` itself) shipped."""
        from conftest import make_report

        assessment_id = uuid.uuid4().hex
        report = make_report(repo_name=repo_name, repo_url=repo_url)
        if assessed_at is not None:
            report.assessed_at = assessed_at
        pool = store._pool
        await pool.execute("ALTER TABLE assessments DISABLE TRIGGER normalize_repo_url_before_write")
        await pool.execute("ALTER TABLE apps DISABLE TRIGGER normalize_repo_url_before_write")
        try:
            await pool.execute(
                """
                INSERT INTO assessments (id, repo_url, repo_name, assessed_at, criticality, overall_score, report_json)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                assessment_id, repo_url, repo_name, report.assessed_at,
                report.criticality, report.overall_score, report.model_dump_json(),
            )
            now = datetime.now(timezone.utc)
            await pool.execute(
                """
                INSERT INTO apps (repo_url, repo_name, created_at, updated_at)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (repo_url) DO NOTHING
                """,
                repo_url, repo_name, now,
            )
        finally:
            await pool.execute("ALTER TABLE assessments ENABLE TRIGGER normalize_repo_url_before_write")
            await pool.execute("ALTER TABLE apps ENABLE TRIGGER normalize_repo_url_before_write")
        return assessment_id

    async def test_merges_a_pre_trigger_dotgit_duplicate_into_one_fleet_row(self, store):
        bare_id = await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/legacy-dup", repo_name="legacy-dup",
        )
        dotgit_id = await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/legacy-dup.git", repo_name="legacy-dup",
        )

        # Sanity: before healing, this really is two distinct Fleet rows --
        # exactly the incident this backs up.
        fleet_before = [r for r in await store.get_fleet_data() if r["repo_name"] == "legacy-dup"]
        assert len(fleet_before) == 2

        merged = await store.dedupe_repo_urls()

        assert merged == [{
            "from": "https://github.com/org/legacy-dup.git",
            "to": "https://github.com/org/legacy-dup",
        }]
        fleet_after = [r for r in await store.get_fleet_data() if r["repo_name"] == "legacy-dup"]
        assert len(fleet_after) == 1
        assert fleet_after[0]["repo_url"] == "https://github.com/org/legacy-dup"
        assert fleet_after[0]["assessment_count"] == 2

        # No assessment history was lost -- both original ids still resolve,
        # and both now agree on the canonical repo_url.
        bare_report = await store.get(bare_id)
        dotgit_report = await store.get(dotgit_id)
        assert bare_report is not None and dotgit_report is not None
        assert bare_report.repo_url == "https://github.com/org/legacy-dup"
        assert dotgit_report.repo_url == "https://github.com/org/legacy-dup"

        apps_rows = await store._pool.fetch(
            "SELECT repo_url FROM apps WHERE repo_name = 'legacy-dup'"
        )
        assert [r["repo_url"] for r in apps_rows] == ["https://github.com/org/legacy-dup"]

    async def test_merges_a_trailing_slash_duplicate_too(self, store):
        await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/slash-dup", repo_name="slash-dup",
        )
        await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/slash-dup/", repo_name="slash-dup",
        )

        merged = await store.dedupe_repo_urls()

        assert merged == [{
            "from": "https://github.com/org/slash-dup/",
            "to": "https://github.com/org/slash-dup",
        }]
        fleet = [r for r in await store.get_fleet_data() if r["repo_name"] == "slash-dup"]
        assert len(fleet) == 1
        assert fleet[0]["assessment_count"] == 2

    async def test_heals_a_single_never_normalized_row_with_no_literal_duplicate(self, store):
        """Even a single row that was only ever written in a non-canonical
        form (no literal duplicate present) gets renamed to the canonical
        form -- not just actual collisions."""
        await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/solo-dotgit.git", repo_name="solo-dotgit",
        )

        merged = await store.dedupe_repo_urls()

        assert merged == [{
            "from": "https://github.com/org/solo-dotgit.git",
            "to": "https://github.com/org/solo-dotgit",
        }]
        row = await store._pool.fetchrow(
            "SELECT repo_url FROM assessments WHERE repo_name = 'solo-dotgit'"
        )
        assert row["repo_url"] == "https://github.com/org/solo-dotgit"

    async def test_is_idempotent_once_healed(self, store):
        await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/idem-dup.git", repo_name="idem-dup",
        )
        first = await store.dedupe_repo_urls()
        assert len(first) == 1
        second = await store.dedupe_repo_urls()
        assert second == []

    async def test_returns_empty_when_nothing_needs_healing(self, store):
        await store.save(_make_report("already-clean-app"))
        assert await store.dedupe_repo_urls() == []

    async def test_merge_keeps_the_newer_infra_repo_url(self, store):
        """`apps.infra_repo_url` merge policy mirrors `_upsert_app`'s own
        "most recent write wins" rule -- whichever variant's `apps` row was
        updated most recently keeps its `infra_repo_url` (falling back to
        the other's if that one is null)."""
        older_at = datetime.now(timezone.utc) - timedelta(days=1)
        newer_at = datetime.now(timezone.utc)
        await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/infra-merge", repo_name="infra-merge",
            assessed_at=older_at,
        )
        await store._pool.execute(
            "UPDATE apps SET infra_repo_url = $1, updated_at = $2 WHERE repo_url = $3",
            "https://github.com/org/infra-old", older_at, "https://github.com/org/infra-merge",
        )
        await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/infra-merge.git", repo_name="infra-merge",
            assessed_at=newer_at,
        )
        await store._pool.execute(
            "UPDATE apps SET infra_repo_url = $1, updated_at = $2 WHERE repo_url = $3",
            "https://github.com/org/infra-new", newer_at, "https://github.com/org/infra-merge.git",
        )

        await store.dedupe_repo_urls()

        row = await store._pool.fetchrow(
            "SELECT infra_repo_url FROM apps WHERE repo_url = 'https://github.com/org/infra-merge'"
        )
        assert row["infra_repo_url"] == "https://github.com/org/infra-new"
        # And the duplicate row is gone, not just orphaned.
        gone = await store._pool.fetchrow(
            "SELECT 1 FROM apps WHERE repo_url = 'https://github.com/org/infra-merge.git'"
        )
        assert gone is None

    async def test_dependents_of_the_merged_variant_survive_the_merge(self, store):
        """Gates/remediations/SLOs/onboarding hanging off the non-canonical
        variant's assessment id must still resolve after the merge --
        they're keyed by `assessment_id`, which never changes; only the
        parent assessment's `repo_url` string does."""
        dotgit_id = await self._insert_legacy_unnormalized_row(
            store, repo_url="https://github.com/org/deps-dup.git", repo_name="deps-dup",
        )
        gate_id = await store.create_gate(dotgit_id, "rollback-review", "gate on the dup variant")

        await store.dedupe_repo_urls()

        gates = await store.list_gates_for_assessment(dotgit_id)
        assert any(g["id"] == gate_id for g in gates)


class TestReapOrphanedJobs:
    """``reap_orphaned_jobs`` fails assess/onboard jobs whose owning process
    (a ``threading.Thread`` or FastAPI ``BackgroundTasks`` coroutine, never
    a persistent queue) died before writing a terminal status -- the real,
    current bug behind a live onboarding progress page stuck forever even
    after the already-shipped client-side stall fallback (commit 2c7c461):
    that fallback correctly re-checks the job's real status, but a job
    orphaned by a pod restart never *has* a terminal status to find."""

    async def test_fails_a_job_stuck_past_max_age(self, store):
        aid = await store.save(_make_report("orphaned-onboard-app"))
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(job_id, "running", "Running onboarding agents...")
        # Simulate the owning pod having died a long time ago: backdate
        # creation past any real deploy/restart cadence.
        await store._pool.execute(
            "UPDATE remediation_jobs SET created_at = $1 WHERE id = $2",
            datetime.now(timezone.utc) - timedelta(hours=1), job_id,
        )

        reaped = await store.reap_orphaned_jobs(max_age_seconds=900)

        assert [r["id"] for r in reaped] == [job_id]
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "failed"
        assert "restart" in job["error"].lower()

    async def test_leaves_a_fresh_running_job_alone(self, store):
        """A job created moments ago on a still-live pod must never be
        reaped just because it hasn't finished yet -- with two replicas
        routinely running, this is the difference between "orphaned" and
        "someone else is still legitimately working on this"."""
        aid = await store.save(_make_report("live-onboard-app"))
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(job_id, "running", "Running onboarding agents...")

        reaped = await store.reap_orphaned_jobs(max_age_seconds=900)

        assert reaped == []
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "running"

    async def test_leaves_terminal_jobs_alone_even_if_old(self, store):
        aid = await store.save(_make_report("completed-old-app"))
        job_id = await store.create_remediation_job(aid)
        await store.update_remediation_job(job_id, "completed", "Onboarding complete")
        await store._pool.execute(
            "UPDATE remediation_jobs SET created_at = $1 WHERE id = $2",
            datetime.now(timezone.utc) - timedelta(hours=1), job_id,
        )

        reaped = await store.reap_orphaned_jobs(max_age_seconds=900)

        assert reaped == []
        job = await store.get_remediation_job(job_id)
        assert job["status"] == "completed"


class TestGates:
    async def test_create_gate_dedupes_pending(self, store):
        assessment_id = await store.save(_make_report())
        id1 = await store.create_gate(assessment_id, "security", "needs review")
        id2 = await store.create_gate(assessment_id, "security", "needs review")
        assert id1 == id2

    async def test_create_gate_dedupes_pending_across_assessments_of_same_app(self, store):
        """Gates are app-scoped: a second assessment of the same repo_url
        must not create a second pending gate of the same type (Actions
        tab ×N / SLO-tracker rollback-review triples)."""
        old_id = await store.save(_make_report("repo-dedupe-gates"))
        id1 = await store.create_gate(old_id, "rollback-review", "breach on v1")
        new_id = await store.save(_make_report("repo-dedupe-gates"))
        assert new_id != old_id
        id2 = await store.create_gate(new_id, "rollback-review", "breach on v2")
        assert id1 == id2
        gates = await store.list_gates_for_assessment(new_id, status="pending")
        assert len([g for g in gates if g["gate_type"] == "rollback-review"]) == 1

    async def test_resolve_gate(self, store):
        assessment_id = await store.save(_make_report())
        gate_id = await store.create_gate(assessment_id, "security", "needs review")
        assert await store.resolve_gate(gate_id, "approved", "alice") is True
        gates = await store.list_all_gates()
        assert gates[0]["status"] == "approved"
        assert gates[0]["resolved_by"] == "alice"

    async def test_resolve_gate_twice_fails(self, store):
        assessment_id = await store.save(_make_report())
        gate_id = await store.create_gate(assessment_id, "security", "needs review")
        await store.resolve_gate(gate_id, "approved", "alice")
        assert await store.resolve_gate(gate_id, "rejected", "bob") is False

    async def test_list_gates_includes_repo_url_for_fleet_attribution(self, store):
        """`fleet.py::_attach_pending_actions` keys gate counts by
        `repo_url`, not `assessment_id` -- `list_gates()` must surface
        `repo_url` on every row for that to work (parity with store.py)."""
        assessment_id = await store.save(_make_report("repo-y"))
        await store.create_gate(assessment_id, "security", "needs review")
        gates = await store.list_gates(status="pending")
        assert gates[0]["repo_url"] == "https://github.com/org/repo-y"

    async def test_create_gate_concurrent_calls_create_only_one_pending_gate(self, store):
        """Regression guard for the check-then-act create_gate race
        (Priority 1c): genuinely concurrent callers for the same app+type
        (e.g. slo-tracker's tick racing a webhook-triggered dispatch) must
        not both see "no pending gate" and both insert one. The advisory
        lock inside create_gate() serializes them instead."""
        import asyncio

        assessment_id = await store.save(_make_report("repo-concurrent-gate"))
        ids = await asyncio.gather(
            *(store.create_gate(assessment_id, "security", "needs review") for _ in range(10))
        )
        assert len(set(ids)) == 1
        gates = await store.list_gates_for_assessment(assessment_id, status="pending")
        assert len([g for g in gates if g["gate_type"] == "security"]) == 1

    async def test_gate_from_old_assessment_visible_after_reassessment(self, store):
        """Orphaned-gate-attribution regression (parity with store.py): a
        gate created against an app's OLD assessment_id must still be
        visible from `list_gates_for_assessment()` called with that same
        app's CURRENT (re-assessed) assessment_id."""
        old_id = await store.save(_make_report("repo-x"))
        gate_id = await store.create_gate(old_id, "security", "needs review")
        new_id = await store.save(_make_report("repo-x"))
        assert new_id != old_id

        gates = await store.list_gates_for_assessment(new_id, status="pending")
        assert [g["id"] for g in gates] == [gate_id]


class TestSlos:
    async def test_save_list_update_delete(self, store):
        assessment_id = await store.save(_make_report())
        slo_id = await store.save_slo(assessment_id, "availability", 99.9)
        slos = await store.list_slos(assessment_id)
        assert len(slos) == 1
        assert slos[0]["id"] == slo_id

        assert await store.update_slo(slo_id, 99.95, "met") is True
        slos = await store.list_slos(assessment_id)
        assert slos[0]["status"] == "met"

        assert await store.delete_slo(slo_id, assessment_id) is True
        assert await store.list_slos(assessment_id) == []

    async def test_delete_slo_wrong_assessment_returns_false(self, store):
        assessment_id = await store.save(_make_report())
        other_id = await store.save(_make_report("other-app"))
        slo_id = await store.save_slo(assessment_id, "availability", 99.9)
        assert await store.delete_slo(slo_id, other_id) is False

    async def test_slo_from_old_assessment_visible_after_reassessment(self, store):
        """Postgres counterpart of the sqlite `store.py` regression test --
        same orphaned-SLO-attribution shape as gates."""
        old_id = await store.save(_make_report("repo-x"))
        slo_id = await store.save_slo(old_id, "availability", 99.9)
        new_id = await store.save(_make_report("repo-x"))
        assert new_id != old_id

        slos = await store.list_slos(new_id)
        assert [s["id"] for s in slos] == [slo_id]

    async def test_list_slos_collapses_identical_rows_across_reassessments(self, store):
        """Identical default SLOs under each historical assessment_id must
        collapse so Fleet SLOs does not show each metric N times."""
        from datetime import datetime, timezone

        assessment_ids: list[str] = []
        for month in (1, 2, 3):
            report = _make_report("slo-triple-app")
            report.assessed_at = datetime(2025, month, 1, tzinfo=timezone.utc)
            aid = await store.save(report)
            assessment_ids.append(aid)
            await store.save_slo(aid, "availability", 99.9)
            await store.save_slo(aid, "error_rate", 1.0)

        slos = await store.list_slos(assessment_ids[-1])
        assert len(slos) == 2
        assert sorted(s["metric_name"] for s in slos) == ["availability", "error_rate"]
        assert {s["assessment_id"] for s in slos} == {assessment_ids[-1]}

    async def test_delete_slo_from_old_assessment_via_new_assessment_id(self, store):
        old_id = await store.save(_make_report("repo-y"))
        slo_id = await store.save_slo(old_id, "availability", 99.9)
        new_id = await store.save(_make_report("repo-y"))

        assert await store.delete_slo(slo_id, new_id) is True
        assert await store.list_slos(new_id) == []


class TestApps:
    """The `apps` table -- see docs/architecture.md's "Data model:
    assessments vs. apps" section for the full rationale."""

    async def test_save_creates_an_apps_row(self, store):
        await store.save(_make_report("new-app"))
        row = await store._pool.fetchrow(
            "SELECT * FROM apps WHERE repo_url = $1", "https://github.com/org/new-app",
        )
        assert row is not None
        assert row["repo_name"] == "new-app"
        assert row["infra_repo_url"] is None

    async def test_set_infra_repo_url_updates_apps_row_even_for_a_non_latest_assessment(self, store):
        old_id = await store.save(_make_report("stale-tab-app"))
        new_id = await store.save(_make_report("stale-tab-app"))
        assert new_id != old_id

        assert await store.set_infra_repo_url(old_id, "https://github.com/org/infra") is True

        fleet_row = next(
            r for r in await store.get_fleet_data()
            if r["repo_url"] == "https://github.com/org/stale-tab-app"
        )
        assert fleet_row["id"] == new_id
        assert fleet_row["infra_repo_url"] == "https://github.com/org/infra"


class TestRemediations:
    async def test_save_list_complete_delete(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_remediation(assessment_id, "hardening", "Add NetworkPolicy")
        remediations = await store.list_remediations(assessment_id)
        assert len(remediations) == 1
        rem_id = remediations[0]["id"]

        assert await store.complete_remediation(rem_id) is True
        remediations = await store.list_remediations(assessment_id)
        assert remediations[0]["status"] == "completed"

        assert await store.delete_remediation(rem_id, assessment_id) is True
        assert await store.list_remediations(assessment_id) == []

    async def test_delete_remediation_wrong_assessment_returns_false(self, store):
        assessment_id = await store.save(_make_report())
        other_id = await store.save(_make_report("other-app"))
        await store.save_remediation(assessment_id, "hardening", "Add NetworkPolicy")
        rem_id = (await store.list_remediations(assessment_id))[0]["id"]
        assert await store.delete_remediation(rem_id, other_id) is False

    async def test_save_remediation_concurrent_calls_create_only_one_live_row(self, store):
        """Regression guard for the check-then-act save_remediation race
        (Priority 1c): genuinely concurrent callers for the same
        (assessment_id, agent_name, description) must not both see "no
        live remediation" and both insert one. The advisory lock inside
        save_remediation() serializes them instead."""
        import asyncio

        assessment_id = await store.save(_make_report())
        ids = await asyncio.gather(
            *(store.save_remediation(assessment_id, "hardening", "Add NetworkPolicy") for _ in range(10))
        )
        assert len(set(ids)) == 1
        remediations = await store.list_remediations(assessment_id)
        assert len(remediations) == 1


class TestAgentRegistry:
    async def test_register_agent_is_idempotent_on_name(self, store):
        await store.register_agent("scanner", "security", capabilities='["scan"]')
        await store.register_agent("scanner", "security", capabilities='["scan", "fix"]')
        agents = await store.list_agents()
        assert len(agents) == 1
        assert agents[0]["capabilities"] == '["scan", "fix"]'

    async def test_register_agent_accepts_prose_capabilities(self, store):
        """Regression guard for a real bug found during the live Postgres
        cutover: AGENT_CAPABILITIES (agents/capabilities.py) passes a
        human-readable prose description, never actual JSON -- e.g.
        "VPA, cost labels, cost report" -- and the column must accept it
        as plain TEXT (matching store.py), not reject it via a `::jsonb`
        cast."""
        await store.register_agent("cost", "cost", capabilities="VPA, cost labels, cost report")
        agents = await store.list_agents()
        agent = next(a for a in agents if a["agent_name"] == "cost")
        assert agent["capabilities"] == "VPA, cost labels, cost report"

    async def test_agent_heartbeat(self, store):
        await store.register_agent("scanner", "security")
        assert await store.agent_heartbeat("scanner") is True

    async def test_agent_heartbeat_upserts_unregistered_agent(self, store):
        """Long-lived watchers (vuln-watcher, slo-tracker, ...) never call
        register_agent -- agent_heartbeat must create the row itself so their
        "last seen" actually shows up on the Agents/Schedules pages."""
        assert await store.agent_heartbeat("vuln-watcher") is True
        agents = await store.list_agents()
        assert any(a["agent_name"] == "vuln-watcher" for a in agents)
        watcher = next(a for a in agents if a["agent_name"] == "vuln-watcher")
        assert watcher["category"] == "watcher"
        assert watcher["last_heartbeat"] is not None

    async def test_agent_heartbeat_custom_category(self, store):
        await store.agent_heartbeat("cve-scanner", category="security")
        agents = await store.list_agents()
        watcher = next(a for a in agents if a["agent_name"] == "cve-scanner")
        assert watcher["category"] == "security"

    async def test_prune_stale_agents_removes_unknown_names(self, store):
        await store.register_agent("security", "hardening")
        await store.register_agent("cost", "cost")

        pruned = await store.prune_stale_agents(known_names={"cost"})

        assert pruned == ["security"]
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"cost"}

    async def test_prune_stale_agents_preserves_known_names(self, store):
        known = {"cost", "dependency", "codechange", "vuln-watcher"}
        for name in known:
            await store.register_agent(name, "test")

        pruned = await store.prune_stale_agents(known_names=known)

        assert pruned == []
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == known


class TestApplyResultsJsonRoundtrip:
    async def test_save_and_get_apply_results(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_apply_results(
            assessment_id,
            {"applied": ["a.yaml"], "skipped": [], "errors": [], "repo_files": ["b.yaml"]},
            namespace="agentit",
            dry_run=False,
        )
        result = await store.get_apply_results(assessment_id)
        assert result["applied"] == ["a.yaml"]
        assert result["repo_files"] == ["b.yaml"]
        assert result["dry_run"] is False


class TestScheduledOperations:
    async def test_toggle_schedule(self, store):
        schedule_id = await store.create_schedule("app", "job", "agent", "* * * * *", "cmd")
        assert await store.toggle_schedule(schedule_id, False) is True
        schedules = await store.list_schedules()
        assert schedules[0]["enabled"] is False

    async def test_has_schedules_for_app(self, store):
        assert await store.has_schedules_for_app("app") is False
        await store.create_schedule("app", "job", "agent", "* * * * *", "cmd")
        assert await store.has_schedules_for_app("app") is True


class TestWebhookDedup:
    async def test_mark_and_check_processed(self, store):
        assert await store.webhook_already_processed("delivery-1") is False
        await store.mark_webhook_processed("delivery-1")
        assert await store.webhook_already_processed("delivery-1") is True
        # ON CONFLICT DO NOTHING must not raise on a repeat delivery id.
        await store.mark_webhook_processed("delivery-1")

    async def test_claim_webhook_first_caller_wins(self, store):
        assert await store.claim_webhook("delivery-claim-1") is True
        assert await store.webhook_already_processed("delivery-claim-1") is True

    async def test_claim_webhook_second_caller_loses(self, store):
        assert await store.claim_webhook("delivery-claim-2") is True
        assert await store.claim_webhook("delivery-claim-2") is False

    async def test_claim_webhook_atomic_under_real_concurrency(self, store):
        """Regression guard for the check-then-act webhook dedup race: fire
        many genuinely concurrent claims for the *same* delivery_id (real
        asyncpg connections from the pool, not a mocked sequential call)
        and assert exactly one wins. Before this fix, callers used
        `webhook_already_processed()` (a SELECT) then, much later,
        `mark_webhook_processed()` (an INSERT) as two separate round trips
        -- concurrent callers could both pass the SELECT before either
        INSERT landed. `claim_webhook()` collapses both into one
        INSERT ... ON CONFLICT DO NOTHING RETURNING, so the database's own
        PRIMARY KEY constraint -- not caller-side timing -- decides the
        single winner."""
        import asyncio

        results = await asyncio.gather(
            *(store.claim_webhook("delivery-race") for _ in range(20))
        )
        assert results.count(True) == 1
        assert results.count(False) == 19


class TestSuppressedChecks:
    async def test_suppress_and_unsuppress(self, store):
        await store.suppress_check("app", "check-a", reason="flaky")
        assert await store.get_suppressed_sources("app") == {"check-a"}
        await store.suppress_check("app", "check-a", reason="still flaky")
        suppressions = await store.get_suppressions("app")
        assert len(suppressions) == 1
        assert suppressions[0]["reason"] == "still flaky"
        await store.unsuppress_check("app", "check-a")
        assert await store.get_suppressed_sources("app") == set()


class TestSkillInventorySnapshots:
    async def test_save_and_get_last_snapshot(self, store):
        assert await store.get_last_skill_inventory_snapshot() is None
        await store.save_skill_inventory_snapshot({"skills": ["a"]})
        await store.save_skill_inventory_snapshot({"skills": ["a", "b"]})
        latest = await store.get_last_skill_inventory_snapshot()
        assert latest["skills"] == ["a", "b"]


class TestPurgeOldData:
    async def test_purge_old_data_no_op_on_fresh_data(self, store):
        await store.save(_make_report())
        counts = await store.purge_old_data(retention_days=30)
        assert all(c == 0 for c in counts.values())

    async def test_purge_deletes_old_events_but_keeps_latest_onboarding(self, store):
        from datetime import timedelta

        assessment_id = await store.save(_make_report())
        old_onboarding_id = await store.save_onboarding(assessment_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])
        new_onboarding_id = await store.save_onboarding(assessment_id, [{"category": "y", "path": "b.yaml", "content": "", "description": ""}])

        old_cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        async with store._pool.acquire() as conn:
            await conn.execute("UPDATE events SET timestamp = $1", old_cutoff)
            await conn.execute("UPDATE onboarding_results SET created_at = $1 WHERE id = $2", old_cutoff, old_onboarding_id)

        counts = await store.purge_old_data(retention_days=30)
        assert counts["events"] >= 1
        assert counts["onboarding_results"] == 1

        remaining_ids = {r["id"] for r in await store.list_onboardings(assessment_id)}
        assert remaining_ids == {new_onboarding_id}


class TestExportAll:
    async def test_export_all_covers_every_table(self, store):
        await store.save(_make_report())
        data = await store.export_all()
        assert "assessments" in data
        assert len(data["assessments"]) == 1
        # Every table export_all() knows about must be represented.
        from agentit.portal.store import _ALL_TABLES
        assert set(data.keys()) == set(_ALL_TABLES)


class TestEventsCorrelationAndAction:
    async def test_log_event_with_correlation_id(self, store):
        await store.log_event("agent", "step-1", "app", "info", "first", correlation_id="chain-1")
        await store.log_event("agent", "step-2", "app", "info", "second", correlation_id="chain-1")
        await store.log_event("agent", "step-3", "app", "info", "unrelated", correlation_id="chain-2")

        chain = await store.list_events_by_correlation_id("chain-1")
        assert [e["action"] for e in chain] == ["step-1", "step-2"]

    async def test_save_populates_correlation_id(self, store):
        """save() must pass its new assessment_id as the correlation_id,
        matching store.py, so an assess -> onboard -> apply chain is
        traceable end to end."""
        assessment_id = await store.save(_make_report())
        chain = await store.list_events_by_correlation_id(assessment_id)
        assert any(e["action"] == "assessment-complete" for e in chain)

    async def test_save_onboarding_populates_correlation_id(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_onboarding(assessment_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])
        chain = await store.list_events_by_correlation_id(assessment_id)
        assert any(e["action"] == "onboarding-complete" for e in chain)

    async def test_list_events_by_action(self, store):
        await store.log_event("agent-a", "decision", "app", "info", "a")
        await store.log_event("agent-b", "decision", "app", "info", "b")
        await store.log_event("agent-c", "other-action", "app", "info", "c")
        rows = await store.list_events_by_action("decision")
        assert len(rows) == 2
        assert {r["agent_id"] for r in rows} == {"agent-a", "agent-b"}


class TestGetEvent:
    """get_event() -- single-row lookup by primary key, backing the
    Self-Improvement tab's per-run drill-through page."""

    async def test_returns_the_matching_event(self, store):
        eid = await store.log_event("capability-scout", "capability-run", None, "info", "proposed something")
        event = await store.get_event(eid)
        assert event is not None
        assert event["id"] == eid
        assert event["summary"] == "proposed something"

    async def test_returns_none_for_unknown_id(self, store):
        assert await store.get_event("does-not-exist") is None


class TestRetryDlqMessage:
    async def test_retry_republishes_to_original_topic(self, store):
        eid = await store.log_event(
            "event-consumer", "dead-letter", "my-app", "error",
            "Dead-lettered from agentit-events: boom",
            details={
                "original_topic": "agentit-events",
                "original_message": {
                    "agentId": "watcher", "action": "tick", "targetApp": "my-app",
                    "severity": "info", "result": {"summary": "hi", "details": {}},
                    "correlationId": "abc123",
                },
                "error": "boom",
            },
        )

        assert await store.retry_dlq_message(eid) is True

        events = await store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "republished to agentit-events" in retry_event["summary"]
        # original dead-letter row is relabelled, not duplicated
        assert await store.list_dlq_messages() == []

    async def test_retry_falls_back_to_relabel_when_no_original_topic(self, store):
        """Rows written before original_topic tracking existed (or a Kafka
        failure) must still mark the row retried instead of silently no-op'ing."""
        eid = await store.log_event(
            "event-consumer", "dead-letter", "my-app", "error", "old-style row",
            details={"original_message": {"foo": "bar"}, "error": "boom"},
        )
        assert await store.retry_dlq_message(eid) is True
        events = await store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "relabelled only" in retry_event["summary"]

    async def test_retry_unknown_event_returns_false(self, store):
        assert await store.retry_dlq_message("nonexistent") is False


class TestAgentRuns:
    async def test_save_and_list_agent_runs(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_agent_run(
            "hardening", "local", "success", assessment_id=assessment_id, duration_ms=120,
        )
        await store.save_agent_run(
            "hardening", "local", "failed", assessment_id=assessment_id, error="boom",
        )

        runs = await store.list_agent_runs("hardening")
        assert len(runs) == 2
        assert runs[0]["status"] == "failed"  # most recent first

        by_assessment = await store.list_agent_runs_for_assessment(assessment_id)
        assert [r["status"] for r in by_assessment] == ["success", "failed"]  # ascending


class TestGetAgentStats:
    """`get_agent_stats()` must read the structured, authoritative
    `agent_runs` table exclusively -- never fall back to a `LIKE
    '%complete%'`/`LIKE '%failed%'` heuristic over raw `events.action`
    strings, which double/under-counts runs (a real, previously-live bug --
    see docs/architecture.md and CLAUDE.md's note on `get_agent_stats()`).
    Migrated from the retired cross-backend `tests/test_store_parity.py`
    now that there is only one backend to test against.
    """

    async def test_matches_agent_runs_ignoring_misleading_events(self, store):
        agent_name = "vuln-watcher"

        # Structured, authoritative agent_runs rows: 3 runs, 2 success, 1
        # failure. This is the only data get_agent_stats() should count.
        for status in ("success", "success", "failed"):
            await store.save_agent_run(agent_name, mode="scheduled", status=status, duration_ms=100)

        # Misleading raw events for the SAME agent, deliberately shaped so
        # a `LIKE '%complete%'`/`LIKE '%failed%'` heuristic over
        # `events.action` would compute *different* totals/success-rate
        # than the true agent_runs-derived numbers above: 4 unrelated
        # 'onboarding-complete' actions (would count as 4 successes under
        # that heuristic, vs. the real 2) and one action matching neither
        # pattern (padding total_events to 5, vs. the real 3). Neither
        # should affect get_agent_stats() -- if it regressed to reading
        # `events` instead of `agent_runs`, this test would see
        # 5/4/0/100.0 instead of 3/2/1/66.7.
        for action in ("onboarding-complete", "onboarding-complete", "onboarding-complete",
                       "onboarding-complete", "watcher-tick"):
            await store.log_event(agent_name, action, None, "info", "noise")

        stats = await store.get_agent_stats(agent_name)
        assert len(stats) == 1
        row = stats[0]
        assert row["total_events"] == 3
        assert row["successes"] == 2
        assert row["failures"] == 1
        assert row["success_rate"] == 66.7

    async def test_no_runs_returns_empty_list(self, store):
        assert await store.get_agent_stats("no-such-agent") == []


class TestCheckResults:
    async def test_save_and_get_check_compliance(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_check_results(assessment_id, [
            {"check_name": "health-check", "dimension": "observability", "passed": True},
            {"check_name": "health-check", "dimension": "observability", "passed": False},
        ])
        compliance = await store.get_check_compliance()
        row = next(r for r in compliance if r["check_name"] == "health-check")
        assert row["total"] == 2
        assert row["passes"] == 1
        assert row["pass_rate"] == 50.0

    async def test_save_check_results_no_op_on_empty_list(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_check_results(assessment_id, [])
        assert await store.get_check_compliance() == []


class TestGetAllFeedback:
    async def test_get_all_feedback_across_apps(self, store):
        await store.record_feedback("app-a", "hardening", "security", "approved")
        await store.record_feedback("app-b", "hardening", "security", "rejected")
        feedback = await store.get_all_feedback()
        assert len(feedback) == 2
        assert {f["app_name"] for f in feedback} == {"app-a", "app-b"}


class TestFleetWideRejectionStats:
    """get_fleet_wide_rejection_stats() -- capability-scout's fleet-wide
    aggregate (docs/self-improvement-for-agentit.md)."""

    async def test_aggregates_rejection_rate_per_category_across_apps(self, store):
        await store.record_feedback("app-a", "hardening", "network-policy", "rejected")
        await store.record_feedback("app-b", "hardening", "network-policy", "rejected")
        await store.record_feedback("app-c", "hardening", "network-policy", "approved")

        stats = await store.get_fleet_wide_rejection_stats()
        by_category = {s["finding_category"]: s for s in stats}
        assert by_category["network-policy"]["total"] == 3
        assert by_category["network-policy"]["rejected"] == 2

    async def test_empty_when_no_feedback(self, store):
        assert await store.get_fleet_wide_rejection_stats() == []


class TestDbStats:
    async def test_get_db_stats_row_counts_and_size(self, store):
        await store.save(_make_report())
        stats = await store.get_db_stats()
        assert stats["row_counts"]["assessments"] == 1
        assert stats["size_bytes"] > 0


class TestBackgroundMaintenanceAsyncHelpers:
    """`app.py::_background_maintenance()`'s three helper calls, exercised
    against a real Postgres-backed store -- regression guard for the bug
    where that loop handed these a nonexistent `.raw` (this class's
    `AssessmentStore` has none, by design) instead of genuinely `await`ing
    them directly."""

    async def test_refresh_db_metrics(self, store):
        from agentit.portal.metrics import refresh_db_metrics

        await store.save(_make_report())
        await refresh_db_metrics(store)  # must not raise

    async def test_prune_stale_agents_and_log(self, store):
        from agentit.agent_registry_cleanup import prune_stale_agents_and_log

        await store.register_agent("chaos", "chaos")
        await store.register_agent("cost", "cost")

        pruned = await prune_stale_agents_and_log(store)

        assert pruned == ["chaos"]
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"cost"}

    async def test_diff_and_log_inventory_changes(self, store, tmp_path):
        from agentit.skill_inventory import diff_and_log_inventory_changes

        skills_dir = tmp_path / "skills"
        checks_dir = tmp_path / "checks"
        domain_dir = skills_dir / "security"
        domain_dir.mkdir(parents=True)
        (domain_dir / "netpol-basic.md").write_text(
            "---\nname: netpol-basic\ndomain: security\nversion: 1\n"
            "triggers: [test]\noutputs: [NetworkPolicy]\n---\nbody\n"
        )

        first = await diff_and_log_inventory_changes(store, skills_dir=skills_dir, checks_dir=checks_dir)
        assert not first.has_changes
        assert await store.get_last_skill_inventory_snapshot() is not None


class TestAssessmentCadence:
    """apps.assessment_cadence -- the per-app setting
    watchers/reassess_scheduler.py's tick loop reads via
    get_apps_due_for_reassessment() to decide which apps to automatically
    re-Assess. See assessments.py::set_assessment_cadence (the portal
    route the Assessment Detail page's dropdown posts to)."""

    async def test_new_app_defaults_to_daily(self, store):
        """'daily' is the schema default (ALTER TABLE ... DEFAULT 'daily')
        so 'default to 24 hours' holds for every app, including ones that
        existed before this column did -- not just ones onboarded after."""
        await store.save(_make_report("cadence-default-app"))
        cadence = await store.get_assessment_cadence("https://github.com/org/cadence-default-app")
        assert cadence == "daily"

    async def test_unknown_repo_url_also_defaults_to_daily(self, store):
        """No apps row at all (e.g. a repo_url that was never assessed) --
        still returns the schema default rather than raising or returning
        None, so callers never need a None-check before comparing."""
        assert await store.get_assessment_cadence("https://github.com/org/never-assessed") == "daily"

    async def test_set_and_get_roundtrip(self, store):
        await store.save(_make_report("cadence-roundtrip-app"))
        repo_url = "https://github.com/org/cadence-roundtrip-app"

        assert await store.set_assessment_cadence(repo_url, "weekly") is True
        assert await store.get_assessment_cadence(repo_url) == "weekly"

        assert await store.set_assessment_cadence(repo_url, "monthly") is True
        assert await store.get_assessment_cadence(repo_url) == "monthly"

    async def test_set_on_unknown_repo_url_returns_false(self, store):
        assert await store.set_assessment_cadence("https://github.com/org/never-assessed", "weekly") is False

    async def test_set_rejects_invalid_cadence(self, store):
        await store.save(_make_report("cadence-invalid-app"))
        with pytest.raises(ValueError):
            await store.set_assessment_cadence("https://github.com/org/cadence-invalid-app", "hourly")


class TestGetAppsDueForReassessment:
    """The real query watchers/reassess_scheduler.py's tick loop runs every
    tick -- must return exactly the apps whose cadence interval has
    elapsed, and nothing else."""

    async def test_freshly_assessed_daily_app_is_not_due(self, store):
        await store.save(_make_report("fresh-daily-app"))
        due = await store.get_apps_due_for_reassessment()
        assert "https://github.com/org/fresh-daily-app" not in {d["repo_url"] for d in due}

    async def test_daily_app_assessed_two_days_ago_is_due(self, store):
        report = _make_report("stale-daily-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        await store.save(report)

        due = await store.get_apps_due_for_reassessment()
        due_row = next(d for d in due if d["repo_url"] == "https://github.com/org/stale-daily-app")
        assert due_row["assessment_cadence"] == "daily"

    async def test_weekly_app_assessed_three_days_ago_is_not_due(self, store):
        report = _make_report("recent-weekly-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=3)
        await store.save(report)
        await store.set_assessment_cadence(report.repo_url, "weekly")

        due = await store.get_apps_due_for_reassessment()
        assert report.repo_url not in {d["repo_url"] for d in due}

    async def test_weekly_app_assessed_ten_days_ago_is_due(self, store):
        report = _make_report("stale-weekly-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=10)
        await store.save(report)
        await store.set_assessment_cadence(report.repo_url, "weekly")

        due = await store.get_apps_due_for_reassessment()
        assert report.repo_url in {d["repo_url"] for d in due}

    async def test_monthly_app_assessed_ten_days_ago_is_not_due(self, store):
        report = _make_report("recent-monthly-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=10)
        await store.save(report)
        await store.set_assessment_cadence(report.repo_url, "monthly")

        due = await store.get_apps_due_for_reassessment()
        assert report.repo_url not in {d["repo_url"] for d in due}

    async def test_manual_cadence_app_is_never_due_regardless_of_age(self, store):
        """The opt-out: even an app that hasn't been assessed in a year
        must never appear here while its cadence is 'manual'."""
        report = _make_report("manual-cadence-app")
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=400)
        await store.save(report)
        await store.set_assessment_cadence(report.repo_url, "manual")

        due = await store.get_apps_due_for_reassessment()
        assert report.repo_url not in {d["repo_url"] for d in due}

    async def test_only_the_latest_assessment_counts_for_staleness(self, store):
        """An app re-assessed recently (a fresh row) must not show as due
        just because an OLDER assessment of the same repo is stale --
        get_fleet_data() already has this same "latest per repo_url"
        guarantee; this query must not regress it."""
        old = _make_report("multi-assessed-app")
        old.assessed_at = datetime.now(timezone.utc) - timedelta(days=5)
        await store.save(old)
        fresh = _make_report("multi-assessed-app")
        fresh.assessed_at = datetime.now(timezone.utc)
        await store.save(fresh)

        due = await store.get_apps_due_for_reassessment()
        assert fresh.repo_url not in {d["repo_url"] for d in due}

    async def test_results_sorted_oldest_first(self, store):
        newer = _make_report("due-app-newer")
        newer.assessed_at = datetime.now(timezone.utc) - timedelta(days=3)
        await store.save(newer)
        older = _make_report("due-app-older")
        older.assessed_at = datetime.now(timezone.utc) - timedelta(days=5)
        await store.save(older)

        due = await store.get_apps_due_for_reassessment()
        due_urls = [d["repo_url"] for d in due]
        assert due_urls.index("https://github.com/org/due-app-older") < due_urls.index(
            "https://github.com/org/due-app-newer"
        )

    async def test_includes_criticality_from_latest_assessment(self, store):
        report = _make_report("critical-due-app")
        report.criticality = "critical"
        report.assessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        await store.save(report)

        due = await store.get_apps_due_for_reassessment()
        due_row = next(d for d in due if d["repo_url"] == report.repo_url)
        assert due_row["criticality"] == "critical"
