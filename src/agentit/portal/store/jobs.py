"""``JobsMixin`` -- the ``remediation_jobs`` table, which despite its name
tracks the async progress of BOTH assess jobs (``create_assessment_job``/
``update_assessment_job``/``claim_continue_onboard``) AND onboarding jobs
(``create_remediation_job``/``update_remediation_job``/``get_remediation_
job``/``list_remediation_jobs``) plus the maintenance sweep that fails any
job orphaned by a process that died mid-run (``reap_orphaned_jobs``).

Kept as one mixin (matching the original file's own two adjacent section
headers, "Assessment Jobs" and "Remediation Jobs") rather than split in
two, since both groups of methods read/write the exact same
``remediation_jobs`` table -- there is no separate table to split along,
and every one of these method names already makes clear which kind of job
it's tracking.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import asyncpg

from ._shared import _affected, _now, _row_to_dict, _rows_to_dicts


class JobsMixin:
    _pool: asyncpg.Pool

    async def create_assessment_job(
        self, repo_url: str, continue_onboard: bool = False,
    ) -> str:
        """Create a tracking job for an async assessment run.

        When ``continue_onboard`` is True, ``steps_completed`` starts with
        ``["continue_onboard"]`` so ``assess_progress`` can chain into
        onboarding after the scorecard is saved (Fleet "Scan").
        """
        job_id = uuid.uuid4().hex
        now = _now()
        steps = '["continue_onboard"]' if continue_onboard else "[]"
        await self._pool.execute(
            """INSERT INTO remediation_jobs
                (id, assessment_id, status, current_step, steps_completed, error, created_at, updated_at)
            VALUES ($1, $2, 'assessing', $3, $4::jsonb, '', $5, $6)""",
            job_id, "", repo_url[:200], steps, now, now,
        )
        return job_id

    async def update_assessment_job(
        self, job_id: str, status: str, step: str = "", assessment_id: str = "",
    ) -> None:
        """Update an assessment job's status and optionally link to the final assessment."""
        now = _now()
        await self._pool.execute(
            """UPDATE remediation_jobs
            SET status = $1, current_step = $2, assessment_id = $3, updated_at = $4
            WHERE id = $5""",
            status, step, assessment_id, now, job_id,
        )

    async def claim_continue_onboard(self, job_id: str) -> bool:
        """Atomically claim a continue_onboard flag on an assessment job.

        Returns True once — later polls of ``/assess/progress`` return False
        so we do not start duplicate onboarding jobs from htmx's 2s refresh.
        """
        now = _now()
        result = await self._pool.execute(
            """
            UPDATE remediation_jobs
            SET steps_completed = '[]'::jsonb, updated_at = $1
            WHERE id = $2
              AND steps_completed @> '["continue_onboard"]'::jsonb
            """,
            now, job_id,
        )
        return _affected(result) > 0

    async def create_remediation_job(self, assessment_id: str) -> str:
        """Create a tracking job for an async onboarding run.

        AutoMode (and the automatic Dry Run -> Deliver chain it used to
        gate) has been removed -- ``steps_completed`` no longer needs an
        ``auto_deliver`` flag; onboarding always stops once manifests are
        saved.
        """
        job_id = uuid.uuid4().hex
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO remediation_jobs
                (id, assessment_id, status, current_step, steps_completed, error, created_at, updated_at)
            VALUES ($1, $2, 'pending', '', '[]'::jsonb, '', $3, $4)
            """,
            job_id, assessment_id, now, now,
        )
        return job_id

    async def update_remediation_job(
        self, job_id: str, status: str, current_step: str = "", error: str = "",
    ) -> None:
        """Read-modify-write on ``steps_completed`` (append ``current_step``
        if not already present) happens inside one transaction with the row
        locked by ``SELECT ... FOR UPDATE`` -- without the row lock, two
        genuinely concurrent callers for the same ``job_id`` (e.g. a
        webhook-retriggered onboard racing the original run's own progress
        update, or a duplicate `BackgroundTasks` dispatch) could both read
        the same ``steps_completed`` array before either writes, each append
        their own step, and whichever commits last silently overwrites the
        other's step -- a lost update, the same class of race the advisory-
        lock/row-lock patterns elsewhere in this file guard against. Postgres
        default `READ COMMITTED` isolation does not prevent this on its own;
        `FOR UPDATE` makes the second transaction's `SELECT` block until the
        first commits, so it reads the already-appended array instead of a
        stale one.
        """
        now = _now()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if current_step:
                    row = await conn.fetchrow(
                        "SELECT steps_completed FROM remediation_jobs WHERE id = $1 FOR UPDATE", job_id,
                    )
                    steps = json.loads(row["steps_completed"]) if row else []
                    if current_step not in steps:
                        steps.append(current_step)
                    await conn.execute(
                        """
                        UPDATE remediation_jobs
                        SET status = $1, current_step = $2, steps_completed = $3::jsonb, error = $4, updated_at = $5
                        WHERE id = $6
                        """,
                        status, current_step, json.dumps(steps), error, now, job_id,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE remediation_jobs
                        SET status = $1, error = $2, updated_at = $3
                        WHERE id = $4
                        """,
                        status, error, now, job_id,
                    )

    async def get_remediation_job(self, job_id: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM remediation_jobs WHERE id = $1", job_id,
        )
        if row is None:
            return None
        d = _row_to_dict(row)
        d["steps_completed"] = json.loads(row["steps_completed"])
        return d

    async def list_remediation_jobs(self, assessment_id: str | None = None) -> list[dict]:
        if assessment_id is not None:
            rows = await self._pool.fetch(
                "SELECT * FROM remediation_jobs WHERE assessment_id = $1 ORDER BY created_at DESC",
                assessment_id,
            )
        else:
            rows = await self._pool.fetch(
                "SELECT * FROM remediation_jobs ORDER BY created_at DESC",
            )
        result = []
        for row in rows:
            d = _row_to_dict(row)
            d["steps_completed"] = json.loads(row["steps_completed"])
            result.append(d)
        return result

    async def reap_orphaned_jobs(self, max_age_seconds: int = 900) -> list[dict]:
        """Fails any assess/onboard job still non-terminal well past every
        real deadline that could keep it legitimately in progress.

        Both ``assess_submit`` (a ``threading.Thread``) and
        ``onboard_submit`` (a FastAPI ``BackgroundTasks`` coroutine) track
        their work in this same table from *within the process that
        started them* -- there's no persistent queue, and nothing resumes
        or re-checks the job if that process dies mid-run (a routine
        rolling deploy, OOM, crash). The row is then orphaned forever at
        its last non-terminal status: no code path ever revisits it, so
        anything polling it (the onboarding SSE stream/progress page,
        assess's progress page) waits on a status that will never change.

        A job that's still genuinely in progress in a live process can
        never be older than ``with_timeout``'s ``OPERATION_TIMEOUT``
        (300s) for the core clone/assess/onboard work, plus a small buffer
        for the fast save/image-build-trigger/webhook steps around it --
        900s leaves a wide margin over that before treating a row as
        orphaned rather than merely slow.
        """
        cutoff = _now() - timedelta(seconds=max_age_seconds)
        rows = await self._pool.fetch(
            """
            UPDATE remediation_jobs
            SET status = 'failed',
                error = 'Interrupted by a service restart before it finished. Please retry.',
                updated_at = $1
            WHERE status NOT IN ('completed', 'failed', 'needs_attention')
              AND created_at < $2
            RETURNING id, assessment_id, current_step
            """,
            _now(), cutoff,
        )
        return _rows_to_dicts(rows)
