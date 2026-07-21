"""``SchedulesMixin`` -- the ``scheduled_operations`` table: recurring
watcher/cron-style job registrations for a given app.

``has_schedules_for_app()`` is included here (rather than left with
``events.py``, where the original file's section-comment placement put it)
since it queries this exact table, not ``events``.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import uuid

import asyncpg

from ._shared import _affected, _now, _rows_to_dicts


class SchedulesMixin:
    _pool: asyncpg.Pool

    async def has_schedules_for_app(self, app_name: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM scheduled_operations WHERE app_name = $1 LIMIT 1", app_name,
        )
        return row is not None

    async def create_schedule(
        self,
        app_name: str,
        job_name: str,
        agent: str,
        schedule: str,
        command: str,
    ) -> str:
        schedule_id = uuid.uuid4().hex
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO scheduled_operations
                (id, app_name, job_name, agent, schedule, command, enabled, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7, $8)
            """,
            schedule_id, app_name, job_name, agent, schedule, command, now, now,
        )
        return schedule_id

    async def list_schedules(self) -> list[dict]:
        rows = await self._pool.fetch("SELECT * FROM scheduled_operations ORDER BY created_at DESC")
        return _rows_to_dicts(rows)

    async def delete_schedule(self, schedule_id: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM scheduled_operations WHERE id = $1", schedule_id,
        )
        return _affected(result) > 0
