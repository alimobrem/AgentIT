"""Tests for ``agentit.migrate_sqlite`` -- the one-time SQLite -> Postgres
import tool for local-dev users with real legacy data (see that module's
docstring; the live cluster deployment never needs this, it's Postgres-only
already).

Builds a real, throwaway ``sqlite3`` file with a representative subset of
the legacy schema (enough tables/columns to exercise FK-order insertion,
JSONB casting, and boolean coercion), migrates it into the real, shared
test Postgres instance (truncated before and after so this doesn't leak
into/from other tests sharing that same session-scoped store), and asserts
against the real rows landed there -- not just that the function returns
without raising.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentit.migrate_sqlite import (
    _MIGRATION_ORDER,
    format_summary,
    migrate_sqlite_to_postgres,
)
from conftest import _get_shared_store, make_store


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


def _build_legacy_sqlite(path: Path, *, with_orphan_child: bool = False) -> dict[str, str]:
    """Create a throwaway SQLite file shaped like the legacy schema, with
    one representative row in each of a handful of tables that exercise
    every translation concern this migration handles: a JSONB-cast text
    column (``assessments.report_json``), an INTEGER-as-boolean column
    (``apply_results.dry_run``), a FK child (``onboarding_results`` ->
    ``assessments``), and a table with a non-``id`` primary key
    (``settings``).

    Returns the ids used, so the test can assert on them after migrating.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("""
            CREATE TABLE assessments (
                id TEXT PRIMARY KEY, repo_url TEXT, repo_name TEXT,
                assessed_at TEXT, criticality TEXT, overall_score REAL,
                report_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE onboarding_results (
                id TEXT PRIMARY KEY, assessment_id TEXT NOT NULL,
                created_at TEXT, files_json TEXT NOT NULL,
                orchestration_json TEXT NOT NULL DEFAULT '{}', pr_url TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE apply_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL,
                namespace TEXT, dry_run INTEGER NOT NULL DEFAULT 0,
                applied_json TEXT NOT NULL DEFAULT '[]',
                skipped_json TEXT NOT NULL DEFAULT '[]',
                errors_json TEXT NOT NULL DEFAULT '[]',
                repo_files_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)
        """)

        assessment_id = f"mig-{uuid.uuid4().hex[:8]}"
        report_json = json.dumps({"repo_url": "https://github.com/org/legacy-app", "overall_score": 42})
        conn.execute(
            "INSERT INTO assessments (id, repo_url, repo_name, assessed_at, criticality, overall_score, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (assessment_id, "https://github.com/org/legacy-app", "legacy-app", _iso(), "high", 42.0, report_json),
        )

        onboarding_id = f"onb-{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO onboarding_results (id, assessment_id, created_at, files_json, orchestration_json, pr_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (onboarding_id, assessment_id, _iso(), json.dumps([{"path": "a.yaml"}]), json.dumps({}), None),
        )

        # dry_run stored as SQLite's 0/1 INTEGER -- must land as a real
        # Postgres BOOLEAN (True), not the integer 1.
        conn.execute(
            "INSERT INTO apply_results (assessment_id, namespace, dry_run, applied_json, skipped_json, errors_json, repo_files_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (assessment_id, "pinky", 1, json.dumps(["a.yaml"]), json.dumps([]), json.dumps([]), json.dumps(["a.yaml"]), _iso()),
        )

        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", ("theme", "dark", _iso()),
        )

        if with_orphan_child:
            # An onboarding_results row whose assessment_id matches nothing
            # in `assessments` -- real-world data-integrity edge case (e.g.
            # a pre-FK-enforcement SQLite file). Postgres's FK constraint
            # should reject this.
            conn.execute(
                "INSERT INTO onboarding_results (id, assessment_id, created_at, files_json, orchestration_json, pr_url) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"onb-{uuid.uuid4().hex[:8]}", "does-not-exist", _iso(), json.dumps([]), json.dumps({}), None),
            )

        conn.commit()
        return {"assessment_id": assessment_id, "onboarding_id": onboarding_id}
    finally:
        conn.close()


async def _truncate_all(dsn: str) -> None:
    """Truncate every store table via the shared session store's own pool
    -- keeps this file's migration writes from leaking into/out of other
    tests that share the same Postgres instance."""
    store = await make_store()
    assert store is not None  # make_store() already truncates before returning


class TestMigrateSqliteToPostgres:
    async def test_migrates_rows_across_all_translation_concerns(self, tmp_path, postgres_dsn):
        await _truncate_all(postgres_dsn)
        sqlite_path = tmp_path / "legacy.db"
        ids = _build_legacy_sqlite(sqlite_path)

        counts = await migrate_sqlite_to_postgres(sqlite_path, postgres_dsn)

        assert counts["assessments"] == 1
        assert counts["onboarding_results"] == 1
        assert counts["apply_results"] == 1
        assert counts["settings"] == 1
        # Tables absent from the legacy file are reported as 0, not omitted.
        assert counts["apps"] == 0
        assert counts["events"] == 0

        # `make_store()` would TRUNCATE the very rows this migration just
        # wrote, so read back via the shared store's pool directly instead.
        store = await _get_shared_store()
        async with store._pool.acquire() as conn:
            report_row = await conn.fetchrow(
                "SELECT repo_name, overall_score, report_json FROM assessments WHERE id = $1",
                ids["assessment_id"],
            )
            assert report_row["repo_name"] == "legacy-app"
            assert report_row["overall_score"] == 42.0
            # report_json must be real, queryable JSONB, not opaque text.
            assert json.loads(report_row["report_json"])["overall_score"] == 42

            onboarding_row = await conn.fetchrow(
                "SELECT files_json FROM onboarding_results WHERE id = $1", ids["onboarding_id"],
            )
            assert json.loads(onboarding_row["files_json"]) == [{"path": "a.yaml"}]

            apply_row = await conn.fetchrow(
                "SELECT dry_run, applied_json FROM apply_results WHERE assessment_id = $1",
                ids["assessment_id"],
            )
            # The real correctness check: SQLite's 0/1 INTEGER must become
            # a genuine Postgres bool, not the integer 1/0.
            assert apply_row["dry_run"] is True
            assert isinstance(apply_row["dry_run"], bool)

            setting_row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", "theme")
            assert setting_row["value"] == "dark"

        await _truncate_all(postgres_dsn)

    async def test_idempotent_rerun_skips_already_migrated_rows(self, tmp_path, postgres_dsn):
        """ON CONFLICT DO NOTHING means a second run against the same
        target reports 0 newly-inserted rows for data already there,
        rather than erroring or duplicating."""
        await _truncate_all(postgres_dsn)
        sqlite_path = tmp_path / "legacy.db"
        _build_legacy_sqlite(sqlite_path)

        first = await migrate_sqlite_to_postgres(sqlite_path, postgres_dsn)
        assert first["assessments"] == 1

        second = await migrate_sqlite_to_postgres(sqlite_path, postgres_dsn)
        assert second["assessments"] == 0
        assert second["onboarding_results"] == 0

        await _truncate_all(postgres_dsn)

    async def test_missing_tables_in_legacy_file_report_zero(self, tmp_path, postgres_dsn):
        """An old SQLite file that predates newer tables (e.g. `apps`,
        `deliveries`) is migrated gracefully -- missing tables count as 0,
        not a hard failure."""
        await _truncate_all(postgres_dsn)
        sqlite_path = tmp_path / "minimal.db"
        conn = sqlite3.connect(str(sqlite_path))
        conn.execute("""
            CREATE TABLE assessments (
                id TEXT PRIMARY KEY, repo_url TEXT, repo_name TEXT,
                assessed_at TEXT, criticality TEXT, overall_score REAL,
                report_json TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO assessments VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a1", "https://github.com/org/x", "x", _iso(), "low", 10.0, json.dumps({})),
        )
        conn.commit()
        conn.close()

        counts = await migrate_sqlite_to_postgres(sqlite_path, postgres_dsn)
        assert counts["assessments"] == 1
        for table in _MIGRATION_ORDER:
            if table != "assessments":
                assert counts[table] == 0

        await _truncate_all(postgres_dsn)

    async def test_orphan_fk_row_raises_instead_of_silently_dropping(self, tmp_path, postgres_dsn):
        """An onboarding_results row whose assessment_id has no matching
        assessment (real-world data-integrity edge case in an old, pre-FK-
        enforcement SQLite file) must surface loudly via Postgres's FK
        constraint, not be silently skipped -- the whole point of not
        wrapping each row in its own try/except is that partial, silently-
        incomplete migrations are worse than a loud failure the operator
        can inspect and fix.
        """
        await _truncate_all(postgres_dsn)
        sqlite_path = tmp_path / "legacy_orphan.db"
        _build_legacy_sqlite(sqlite_path, with_orphan_child=True)

        import asyncpg

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await migrate_sqlite_to_postgres(sqlite_path, postgres_dsn)

        await _truncate_all(postgres_dsn)

    def test_raises_on_missing_sqlite_file(self, tmp_path, postgres_dsn):
        import asyncio

        with pytest.raises(FileNotFoundError):
            asyncio.run(migrate_sqlite_to_postgres(tmp_path / "nope.db", postgres_dsn))

    def test_format_summary_lists_nonzero_tables_and_total(self):
        summary = format_summary({"assessments": 3, "apps": 0, "gates": 2})
        assert "assessments: 3" in summary
        assert "gates: 2" in summary
        assert "apps:" not in summary  # zero-row tables omitted from the per-table listing
        assert "Total: 5 row(s)" in summary
