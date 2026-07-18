"""One-time migration for anyone with local SQLite data from before Postgres
became the only supported store.

**Why this exists:** the live cluster deployment already runs Postgres
exclusively (confirmed via `argocd/application.yaml`) and never needs this
-- there is no SQLite data on that deployment to migrate. This module
exists purely for local-dev/test users who accumulated real data (past
assessments, onboarding results, event history, feedback, etc.) in a
SQLite file under the old default backend, before this repo dropped
SQLite support entirely. If your local `agentit.db` is disposable (the
common case -- see `docs/postgres-migration-plan.md`'s §6 "clean cutover,
no automated data migration" decision, which was written for the same
reason at the code level), you don't need this: just delete the file and
start fresh against Postgres.

Usage::

    agentit migrate-sqlite-to-postgres --sqlite-path ./agentit.db --dsn postgresql://...

Approach: read every row out of the old SQLite file with the stdlib
`sqlite3` module directly (no dependency on the removed synchronous
`AssessmentStore` class), insert it into the already-migrated Postgres
schema (`AssessmentStore.create()` already runs `SCHEMA_SQL` idempotently)
table by table, in FK-dependency order, translating SQLite's types to
Postgres's per the mapping `docs/postgres-migration-plan.md` §4 documented
during the original cutover (ISO-8601 TEXT -> TIMESTAMPTZ needs an explicit
`datetime.fromisoformat()` parse -- unlike Postgres's own text-to-timestamp
casting, `asyncpg`'s binary protocol rejects a plain `str` for a
`TIMESTAMPTZ` column outright (`DataError: expected a datetime.date or
datetime.datetime instance, got 'str'`), confirmed by running this
migration against a real Postgres instance rather than just inspecting it;
JSON TEXT blobs are inserted with an explicit `::jsonb` cast; SQLite's
`0`/`1` INTEGER booleans are coerced to real Python `bool`).

This is intentionally NOT wired into `AssessmentStore` itself -- it is a
standalone, one-shot tool, not a runtime code path, and it never needs to
run more than once per person who has real data worth preserving.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Order matters: parents (referenced by FOREIGN KEY) before children.
# Mirrors store.py's SCHEMA_SQL dependency graph -- every *_id column that
# is a `REFERENCES assessments(id)` must be inserted after `assessments`.
_MIGRATION_ORDER = [
    "assessments",
    "apps",
    "onboarding_results",
    "events",
    "gates",
    "agent_registry",
    "slos",
    "apply_results",
    "settings",
    "remediation_jobs",
    "scheduled_operations",
    "processed_webhooks",
    "agent_feedback",
    "skill_effectiveness",
    "suppressed_checks",
    "skill_inventory_snapshots",
    "agent_runs",
    "check_results",
    "deliveries",
]

# Columns that are JSON-blob TEXT in SQLite and JSONB in Postgres -- these
# need an explicit `::jsonb` cast on insert (a plain positional `$n` with a
# Python str is inserted as TEXT-typed data by asyncpg, which Postgres will
# then reject for a JSONB column unless the placeholder itself is cast).
_JSONB_COLUMNS: dict[str, set[str]] = {
    "assessments": {"report_json"},
    "onboarding_results": {"files_json", "orchestration_json"},
    "events": {"details_json"},
    "apply_results": {"applied_json", "skipped_json", "errors_json", "repo_files_json"},
    "remediation_jobs": {"steps_completed"},
    "deliveries": {"categories_json", "details_json"},
    "skill_inventory_snapshots": {"snapshot_json"},
}

# Columns that are INTEGER-as-boolean in SQLite and real BOOLEAN in
# Postgres -- see docs/postgres-migration-plan.md §4's type-mapping table.
_BOOLEAN_COLUMNS: dict[str, set[str]] = {
    "apply_results": {"dry_run"},
    "scheduled_operations": {"enabled"},
    "check_results": {"passed"},
}

# Scalar (non-JSONB, non-boolean) columns that are `NOT NULL DEFAULT
# '<literal>'` in `SCHEMA_SQL` -- a legacy SQLite row can genuinely have
# a stored `NULL` for one of these (e.g. `pr_url` was added later via
# `ALTER TABLE ... ADD COLUMN pr_url TEXT DEFAULT ''`, and SQLite's `NOT
# NULL`-less `ALTER TABLE ADD COLUMN` never enforced the "no NULL" rule
# store.py's read path already coalesces around -- see `get_onboarding()`'s
# `r["pr_url"] or ""`). Postgres's column `DEFAULT` only applies when a
# column is *omitted* from an `INSERT`, not when `NULL` is passed
# explicitly for it -- since this migration always inserts every column
# present in the source row, an explicit `NULL` here would otherwise raise
# `NotNullViolationError` instead of falling back to the same default the
# live schema declares.
_TEXT_DEFAULT_COLUMNS: dict[str, dict[str, str]] = {
    "onboarding_results": {"pr_url": ""},
    "events": {"severity": "info"},
    "gates": {"status": "pending"},
    "agent_registry": {"status": "active", "capabilities": "[]"},
    "slos": {"status": "unknown"},
    "remediation_jobs": {"assessment_id": "", "status": "pending", "current_step": "", "error": ""},
    "suppressed_checks": {"suppressed_by": "user"},
    "agent_runs": {"mode": "local"},
    "deliveries": {"status": "pending", "verification": "unknown"},
}

# Columns that are ISO-8601 TEXT in SQLite and TIMESTAMPTZ in Postgres --
# mirrors every `TIMESTAMPTZ` column declared in `store.py`'s `SCHEMA_SQL`.
# `asyncpg` requires a real `datetime` object for these (a plain `str`
# raises `DataError` at insert time, confirmed against a real Postgres
# instance -- unlike the JSONB columns above, there is no `::timestamptz`
# text-cast escape hatch needed here since parsing in Python is simpler
# and just as correct).
_TIMESTAMP_COLUMNS: dict[str, set[str]] = {
    "assessments": {"assessed_at"},
    "apps": {"created_at", "updated_at"},
    "onboarding_results": {"created_at"},
    "events": {"timestamp"},
    "gates": {"created_at", "resolved_at"},
    "agent_registry": {"last_heartbeat", "registered_at"},
    "slos": {"created_at", "updated_at"},
    "apply_results": {"created_at"},
    "settings": {"updated_at"},
    "remediation_jobs": {"created_at", "updated_at"},
    "scheduled_operations": {"created_at", "updated_at"},
    "processed_webhooks": {"processed_at"},
    "agent_feedback": {"created_at"},
    "skill_effectiveness": {"created_at"},
    "suppressed_checks": {"created_at"},
    "skill_inventory_snapshots": {"created_at"},
    "agent_runs": {"started_at"},
    "check_results": {"created_at"},
    "deliveries": {"created_at", "updated_at"},
}

# Fallback default for a NULL JSONB column value -- matches each column's
# real `DEFAULT` in SCHEMA_SQL. Array-shaped columns default to `[]`,
# everything else to `{}`.
_JSONB_ARRAY_DEFAULTS: set[str] = {
    "files_json", "applied_json", "skipped_json", "errors_json",
    "repo_files_json", "steps_completed",
}

# Tables whose PK is `BIGINT GENERATED ALWAYS AS IDENTITY` in Postgres
# (SQLite's `INTEGER PRIMARY KEY AUTOINCREMENT` equivalent -- see
# docs/postgres-migration-plan.md §4's type-mapping table). A plain
# `INSERT` supplying an explicit value for a `GENERATED ALWAYS` column is
# rejected outright (`GeneratedAlwaysError`) -- confirmed against a real
# Postgres instance -- so these need `OVERRIDING SYSTEM VALUE` to preserve
# the legacy numeric ids (there's nothing semantically important about the
# exact values, but keeping them stable is what makes `ON CONFLICT (id) DO
# NOTHING` a correct idempotency check on a second run, the same as every
# other table). The sequence backing the column is advanced past the
# highest migrated id afterward so the *next* real `AssessmentStore` write
# doesn't try to reuse an id a migrated row already claimed.
_IDENTITY_PK_TABLES: set[str] = {"apply_results", "skill_inventory_snapshots", "check_results"}


def _read_sqlite_tables(sqlite_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read every migratable table from a SQLite file into plain dicts.

    Tables that don't exist in an older SQLite file (e.g. `apps`,
    `deliveries`, `agent_runs`, `check_results` were all added after the
    original SQLite schema shipped) are skipped gracefully rather than
    failing the whole migration -- an old database simply has nothing to
    contribute for that table.
    """
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        existing = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        data: dict[str, list[dict[str, Any]]] = {}
        for table in _MIGRATION_ORDER:
            if table not in existing:
                data[table] = []
                continue
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            data[table] = [dict(r) for r in rows]
        return data
    finally:
        conn.close()


async def migrate_sqlite_to_postgres(
    sqlite_path: Path,
    dsn: str,
    *,
    on_progress: Any = None,
) -> dict[str, int]:
    """Copy every row from a legacy SQLite `AssessmentStore` file into a
    real Postgres database, table by table, in FK-dependency order.

    Idempotent per row via each table's real primary key / unique
    constraint (`ON CONFLICT ... DO NOTHING`) -- safe to re-run if it's
    interrupted partway through; already-migrated rows are simply skipped
    on a second pass rather than erroring or duplicating.

    Returns a dict of table name -> number of rows inserted (rows already
    present, and therefore skipped by `ON CONFLICT DO NOTHING`, are not
    counted).
    """
    import asyncpg

    from agentit.portal.store import SCHEMA_SQL

    if not sqlite_path.is_file():
        raise FileNotFoundError(f"No SQLite file at {sqlite_path}")

    tables = _read_sqlite_tables(sqlite_path)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await pool.execute(SCHEMA_SQL)

        counts: dict[str, int] = {}
        async with pool.acquire() as conn:
            for table in _MIGRATION_ORDER:
                rows = tables.get(table, [])
                if not rows:
                    counts[table] = 0
                    if on_progress:
                        on_progress(table, 0, 0)
                    continue

                jsonb_cols = _JSONB_COLUMNS.get(table, set())
                bool_cols = _BOOLEAN_COLUMNS.get(table, set())
                ts_cols = _TIMESTAMP_COLUMNS.get(table, set())
                text_defaults = _TEXT_DEFAULT_COLUMNS.get(table, {})
                columns = list(rows[0].keys())

                placeholders = []
                for i, col in enumerate(columns, start=1):
                    if col in jsonb_cols:
                        placeholders.append(f"${i}::jsonb")
                    else:
                        placeholders.append(f"${i}")

                # `settings`/`suppressed_checks` etc. each have their own
                # real primary key or unique constraint already declared
                # in SCHEMA_SQL; ON CONFLICT DO NOTHING against that
                # constraint is what makes re-running this script safe.
                conflict_col = _primary_key_column(table)
                overriding = "OVERRIDING SYSTEM VALUE " if table in _IDENTITY_PK_TABLES else ""
                sql = (
                    f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608
                    f"{overriding}VALUES ({', '.join(placeholders)}) "
                    f"ON CONFLICT ({conflict_col}) DO NOTHING"
                )

                inserted = 0
                for row in rows:
                    values = []
                    for col in columns:
                        v = row[col]
                        if col in bool_cols and v is not None:
                            v = bool(v)
                        elif col in jsonb_cols and v is None:
                            v = "[]" if col in _JSONB_ARRAY_DEFAULTS else "{}"
                        elif col in ts_cols and isinstance(v, str):
                            v = datetime.fromisoformat(v)
                        elif v is None and col in text_defaults:
                            v = text_defaults[col]
                        values.append(v)
                    status = await conn.execute(sql, *values)
                    if status.rsplit(" ", 1)[-1] not in ("0",):
                        inserted += 1
                counts[table] = inserted
                if on_progress:
                    on_progress(table, inserted, len(rows))

                if table in _IDENTITY_PK_TABLES and inserted:
                    # Advance the identity sequence past whatever we just
                    # inserted with an explicit id, so the next real
                    # AssessmentStore write generates a fresh, non-colliding
                    # value instead of trying to reuse one a migrated row
                    # already claimed.
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "  # noqa: S608
                        f"(SELECT MAX(id) FROM {table}))"  # noqa: S608
                    )
        return counts
    finally:
        await pool.close()


def _primary_key_column(table: str) -> str:
    """The column each table's `ON CONFLICT` should target -- matches the
    real PRIMARY KEY/UNIQUE constraint SCHEMA_SQL declares for that table.
    `skill_effectiveness` has a composite key; the others are all a single
    `id`-shaped column (or `key`/`delivery_id`/`repo_url` for the handful
    of tables that don't use a synthetic `id`).
    """
    return {
        "settings": "key",
        "processed_webhooks": "delivery_id",
        "apps": "repo_url",
        "skill_effectiveness": "skill_name, app_name, created_at",
        "apply_results": "id",
        "skill_inventory_snapshots": "id",
        "check_results": "id",
    }.get(table, "id")


def format_summary(counts: dict[str, int]) -> str:
    lines = ["Migration complete. Rows inserted per table:"]
    total = 0
    for table, n in counts.items():
        if n:
            lines.append(f"  {table}: {n}")
        total += n
    lines.append(f"Total: {total} row(s) inserted (already-present rows were skipped).")
    return "\n".join(lines)
