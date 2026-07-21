"""``SLOsMixin`` -- the ``slos`` table: per-app health-metric targets and
their current status, scoped by the app's ``repo_url`` across every
historical assessment (the same "apps outlive a single assessment run"
convention ``deliveries``/``onboarding_results`` also follow).

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import uuid

import asyncpg

from ._shared import _affected, _now, _rows_to_dicts


class SLOsMixin:
    _pool: asyncpg.Pool

    async def save_slo(
        self, assessment_id: str, metric_name: str, target_value: float
    ) -> str:
        slo_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO slos (id, assessment_id, metric_name, target_value, status, created_at)
            VALUES ($1, $2, $3, $4, 'unknown', $5)
            """,
            slo_id, assessment_id, metric_name, target_value, _now(),
        )
        return slo_id

    async def list_slos(self, assessment_id: str) -> list[dict]:
        """Keyed off ``repo_url``, joined back through every historical
        assessment of the same app, the same fix shape
        ``list_deliveries_for_app()`` already has.

        Identical ``(metric_name, target_value)`` rows from repeated
        onboarding (before default-SLO seeding skipped existing metrics)
        are collapsed to the newest assessment that owns that identity,
        so Fleet / per-app SLO pages do not show each metric N times.
        Multiple rows with the same identity on a *single* assessment
        (Add-SLO / progress-bar fixtures) are preserved.
        """
        rows = await self._pool.fetch(
            """
            WITH scoped AS (
                SELECT slos.*, assessments.assessed_at
                FROM slos
                INNER JOIN assessments ON slos.assessment_id = assessments.id
                WHERE assessments.repo_url = (
                    SELECT repo_url FROM assessments WHERE id = $1
                )
            ),
            newest AS (
                SELECT metric_name, target_value, MAX(assessed_at) AS max_at
                FROM scoped
                GROUP BY metric_name, target_value
            )
            SELECT s.id, s.assessment_id, s.metric_name, s.target_value,
                   s.current_value, s.status, s.created_at, s.updated_at
            FROM scoped s
            INNER JOIN newest n
              ON s.metric_name = n.metric_name
             AND s.target_value = n.target_value
             AND s.assessed_at = n.max_at
            ORDER BY s.metric_name
            """,
            assessment_id,
        )
        return _rows_to_dicts(rows)

    async def update_slo(
        self, slo_id: str, current_value: float, status: str
    ) -> bool:
        result = await self._pool.execute(
            """
            UPDATE slos SET current_value = $1, status = $2, updated_at = $3
            WHERE id = $4
            """,
            current_value, status, _now(), slo_id,
        )
        return _affected(result) > 0

    async def delete_slo(self, slo_id: str, assessment_id: str) -> bool:
        """Scoped by the app's ``repo_url``, not an exact ``assessment_id``
        match."""
        result = await self._pool.execute(
            """
            DELETE FROM slos
            WHERE id = $1
              AND assessment_id IN (
                  SELECT id FROM assessments
                  WHERE repo_url = (SELECT repo_url FROM assessments WHERE id = $2)
              )
            """,
            slo_id, assessment_id,
        )
        return _affected(result) > 0
