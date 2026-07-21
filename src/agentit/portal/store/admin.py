"""``AdminMixin`` -- the ``settings`` key/value table, plus the three
cross-cutting maintenance operations that read/write across every table in
the database rather than owning one of their own: ``export_all()``
(disaster-recovery/migration JSON dump, walks ``_ALL_TABLES``),
``purge_old_data()`` (retention-window cleanup across several tables), and
``get_db_stats()`` (Prometheus row-count/size gauges, walks
``_METRIC_TABLES``).

Grouped together as "the operator-facing admin surface" rather than split
further -- ``settings`` itself is a tiny, generic key/value store with no
domain of its own (``purge_old_data()``'s retention-days value is one of
its few real readers, via ``routes/settings.py``), and the three
maintenance operations have no natural table to live alongside since each
one deliberately spans many.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``. ``purge_old_data()``
also calls ``self.log_event()`` (``events.py``) -- resolved through normal
attribute lookup on the combined ``AssessmentStore`` instance, per this
package's mixin-composition pattern (see ``store/__init__.py``'s module
docstring).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import asyncpg

from ._shared import _ALL_TABLES, _affected, _now, _rows_to_dicts

logger = logging.getLogger(__name__)


class AdminMixin:
    _pool: asyncpg.Pool

    _METRIC_TABLES = (
        "assessments", "apps", "onboarding_results", "events",
        "agent_registry", "slos", "apply_results", "remediation_jobs",
        "scheduled_operations", "agent_feedback", "skill_effectiveness",
        "agent_runs", "check_results", "deliveries", "pr_outcomes",
    )

    async def get_setting(self, key: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT value FROM settings WHERE key = $1", key,
        )
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            key, value, _now(),
        )

    async def list_settings(self) -> list[dict]:
        rows = await self._pool.fetch("SELECT * FROM settings ORDER BY key")
        return _rows_to_dicts(rows)

    async def export_all(self) -> dict:
        """Export all tables as JSON for disaster recovery (and as the seed
        source for `agentit migrate-sqlite-to-postgres`'s SQLite-side read --
        see `agentit/migrate_sqlite.py`)."""
        result = {}
        for table in _ALL_TABLES:
            try:
                rows = await self._pool.fetch(f"SELECT * FROM {table}")
                result[table] = _rows_to_dicts(rows)
            except Exception:
                result[table] = []
        return result

    async def purge_old_data(self, retention_days: int = 30) -> dict[str, int]:
        """Delete data older than retention_days. Returns count of deleted rows per table."""
        cutoff = _now() - timedelta(days=retention_days)
        counts: dict[str, int] = {}

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for table, col in [
                    ("events", "timestamp"),
                    ("remediation_jobs", "created_at"),
                    ("apply_results", "created_at"),
                    ("agent_runs", "started_at"),
                    ("check_results", "created_at"),
                ]:
                    status = await conn.execute(
                        f"DELETE FROM {table} WHERE {col} < $1", cutoff,
                    )
                    counts[table] = _affected(status)

                # "Keep the latest row per assessment_id" needs DISTINCT ON
                # (Postgres rejects selecting a non-grouped, non-aggregated
                # column in a GROUP BY query).
                status = await conn.execute(
                    "DELETE FROM onboarding_results WHERE created_at < $1 AND id NOT IN "
                    "(SELECT DISTINCT ON (assessment_id) id FROM onboarding_results "
                    "ORDER BY assessment_id, created_at DESC)",
                    cutoff,
                )
                counts["onboarding_results"] = _affected(status)

                webhook_cutoff = _now() - timedelta(days=7)
                status = await conn.execute(
                    "DELETE FROM processed_webhooks WHERE processed_at < $1",
                    webhook_cutoff,
                )
                counts["processed_webhooks"] = _affected(status)

        total = sum(counts.values())
        if total > 0:
            await self.log_event(
                "store", "data-purged", None, "info",
                f"Purged {total} stale rows (retention={retention_days}d): "
                + ", ".join(f"{t}={c}" for t, c in counts.items() if c > 0),
            )
        return counts

    async def get_db_stats(self) -> dict:
        """Row counts per table plus the database size, for the
        `agentit_db_size_bytes` / `agentit_db_rows` Prometheus gauges.

        `pg_database_size()` is the Postgres equivalent of "how big is this
        database right now" -- there's no single on-disk "file" to size the
        way a SQLite deployment would have.
        """
        row_counts: dict[str, int] = {}
        for table in self._METRIC_TABLES:
            try:
                row_counts[table] = await self._pool.fetchval(f"SELECT COUNT(*) FROM {table}") or 0
            except asyncpg.PostgresError:
                logger.debug("Failed to count rows in table %s", table, exc_info=True)
                row_counts[table] = 0

        size_bytes = 0
        try:
            size_bytes = await self._pool.fetchval(
                "SELECT pg_database_size(current_database())"
            ) or 0
        except asyncpg.PostgresError:
            logger.debug("Failed to fetch Postgres database size", exc_info=True)

        return {"row_counts": row_counts, "size_bytes": size_bytes}
