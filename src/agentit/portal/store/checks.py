"""``ChecksMixin`` -- the ``check_results`` table (per-check pass/fail
snapshots recorded for every assessment, backing fleet-wide compliance
reporting) the ``suppressed_checks`` table (a human's explicit opt-out
of a specific check for a specific app), and ``secret_classify_cache``
(persisted LLM secret false-positive verdicts keyed by content hash so
repeat Scans skip LLM + Decisions spam).

Grouped together since both are about the data-driven check engine's
per-app/fleet-wide state -- distinct from ``skills.py``'s skill-outcome
learning loop, even though both are, broadly, "quality signal" tables.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import asyncpg

from ._shared import _now, _row_to_dict, _rows_to_dicts


class ChecksMixin:
    _pool: asyncpg.Pool

    async def save_check_results(self, assessment_id: str, results: list[dict]) -> None:
        """Persist per-check pass/fail rows for one assessment.

        `results` is a list of ``{"check_name": ..., "dimension": ..., "passed": bool}``
        dicts, as produced by ``check_engine.run_checks_with_status``.
        """
        if not results:
            return
        now = _now()
        await self._pool.executemany(
            """
            INSERT INTO check_results (assessment_id, check_name, dimension, passed, created_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            [
                (assessment_id, r["check_name"], r["dimension"], bool(r["passed"]), now)
                for r in results
            ],
        )

    async def get_check_compliance(self) -> list[dict]:
        """Fleet-wide check compliance: pass rate per check, across every
        recorded assessment snapshot."""
        rows = await self._pool.fetch(
            """
            SELECT check_name, dimension,
                   SUM(CASE WHEN passed THEN 1 ELSE 0 END) as passes,
                   COUNT(*) as total
            FROM check_results
            GROUP BY check_name, dimension
            ORDER BY dimension, check_name
            """
        )
        result = []
        for r in rows:
            total = r["total"] or 0
            pass_rate = (r["passes"] / total * 100) if total > 0 else 0
            result.append({
                "check_name": r["check_name"],
                "dimension": r["dimension"],
                "passes": r["passes"],
                "total": total,
                "pass_rate": round(pass_rate, 1),
            })
        return result

    async def suppress_check(self, app_name: str, check_source: str, reason: str = "") -> None:
        await self._pool.execute(
            "INSERT INTO suppressed_checks (id, app_name, check_source, reason, created_at) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (app_name, check_source) DO UPDATE SET reason = EXCLUDED.reason, created_at = EXCLUDED.created_at",
            f"{app_name}:{check_source}", app_name, check_source, reason, _now(),
        )

    async def unsuppress_check(self, app_name: str, check_source: str) -> None:
        await self._pool.execute(
            "DELETE FROM suppressed_checks WHERE app_name = $1 AND check_source = $2",
            app_name, check_source,
        )

    async def get_suppressions(self, app_name: str) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT check_source, reason, suppressed_by, created_at "
            "FROM suppressed_checks WHERE app_name = $1 ORDER BY created_at DESC",
            app_name,
        )
        return _rows_to_dicts(rows)

    async def lookup_secret_classify(
        self, app_name: str, file_path: str, snippet_hash: str,
    ) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT app_name, file_path, snippet_hash, secret_type, outcome, "
            "confidence, reason, source, first_seen_at, last_seen_at, hit_count "
            "FROM secret_classify_cache "
            "WHERE app_name = $1 AND file_path = $2 AND snippet_hash = $3",
            app_name, file_path, snippet_hash,
        )
        return _row_to_dict(row)

    async def upsert_secret_classify(
        self,
        app_name: str,
        file_path: str,
        snippet_hash: str,
        secret_type: str,
        outcome: str,
        confidence: float,
        reason: str,
        source: str = "llm",
    ) -> None:
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO secret_classify_cache (
                app_name, file_path, snippet_hash, secret_type, outcome,
                confidence, reason, source, first_seen_at, last_seen_at, hit_count
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, 1)
            ON CONFLICT (app_name, file_path, snippet_hash) DO UPDATE SET
                secret_type = EXCLUDED.secret_type,
                outcome = EXCLUDED.outcome,
                confidence = EXCLUDED.confidence,
                reason = EXCLUDED.reason,
                source = EXCLUDED.source,
                last_seen_at = EXCLUDED.last_seen_at,
                hit_count = secret_classify_cache.hit_count + 1
            """,
            app_name, file_path, snippet_hash, secret_type, outcome,
            float(confidence), reason, source, now,
        )

    async def touch_secret_classify(
        self, app_name: str, file_path: str, snippet_hash: str,
    ) -> None:
        await self._pool.execute(
            """
            UPDATE secret_classify_cache
            SET last_seen_at = $4, hit_count = hit_count + 1
            WHERE app_name = $1 AND file_path = $2 AND snippet_hash = $3
            """,
            app_name, file_path, snippet_hash, _now(),
        )

    async def delete_secret_classify(
        self, app_name: str, file_path: str, snippet_hash: str,
    ) -> None:
        await self._pool.execute(
            "DELETE FROM secret_classify_cache "
            "WHERE app_name = $1 AND file_path = $2 AND snippet_hash = $3",
            app_name, file_path, snippet_hash,
        )

