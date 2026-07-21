"""``FeedbackMixin`` -- the ``agent_feedback`` table (explicit human
approve/reject/modify actions on an agent recommendation) and the
``pr_outcomes`` table (durable, real-GitHub-derived evidence of what
happened to a PR AgentIT opened -- rejected, or merged with a pre-merge
edit). Both are "signal about how well a generated fix/recommendation
actually landed," feeding the same eventual "learn from this" mechanism
(see ``pr_outcomes.py``'s module docstring) -- grouped together for that
reason rather than split by table.

``get_human_override()`` lives here (not split out) since it reads
``agent_feedback`` -- the same table ``record_feedback()``/
``get_all_feedback()``/``get_rejection_count()`` already own.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import json
import uuid

import asyncpg

from ._shared import _now, _pr_outcome_row_to_dict, _rows_to_dicts


class FeedbackMixin:
    _pool: asyncpg.Pool

    async def record_feedback(
        self,
        app_name: str,
        agent_name: str,
        finding_category: str,
        action: str,
        human_reason: str = "",
        original_value: str = "",
        human_value: str = "",
    ) -> str:
        """Record human feedback on an agent recommendation."""
        feedback_id = uuid.uuid4().hex
        await self._pool.execute(
            """INSERT INTO agent_feedback (id, app_name, agent_name, finding_category,
               action, human_reason, original_value, human_value, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            feedback_id, app_name, agent_name, finding_category, action,
            human_reason, original_value, human_value, _now(),
        )
        return feedback_id

    async def get_all_feedback(self, limit: int = 50) -> list[dict]:
        """Fleet-wide feedback history across all apps, most recent first.

        Used by the Insights page — the now-deleted ``get_feedback_for_app("")``
        used to filter on ``WHERE app_name = ''`` and always return nothing
        useful, so this is the fleet-wide equivalent for that view.
        """
        rows = await self._pool.fetch(
            "SELECT * FROM agent_feedback ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return _rows_to_dicts(rows)

    async def get_rejection_count(self, app_name: str, finding_category: str) -> int:
        """How many times has this category been rejected for this app?"""
        row = await self._pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM agent_feedback WHERE app_name = $1 AND finding_category = $2 AND action = 'rejected'",
            app_name, finding_category,
        )
        return row["cnt"] if row else 0

    async def get_fleet_wide_rejection_stats(self, limit: int = 10) -> list[dict]:
        """Rejection counts per finding category, across every app."""
        rows = await self._pool.fetch(
            """
            SELECT finding_category,
                   COUNT(*) as total,
                   SUM(CASE WHEN action = 'rejected' THEN 1 ELSE 0 END) as rejected
            FROM agent_feedback
            GROUP BY finding_category
            ORDER BY rejected DESC
            LIMIT $1
            """,
            limit,
        )
        result = []
        for r in rows:
            total = r["total"] or 0
            rejected = r["rejected"] or 0
            result.append({
                "finding_category": r["finding_category"],
                "total": total,
                "rejected": rejected,
                "rejection_rate": round((rejected / total * 100) if total > 0 else 0, 1),
            })
        return result

    async def record_pr_outcome(
        self,
        pr_url: str,
        app_name: str,
        outcome: str,
        *,
        assessment_id: str | None = None,
        category: str = "",
        finding_category: str = "",
        skill_names: list[str] | None = None,
        reject_reason: str = "",
        edit_diff: list[dict] | None = None,
    ) -> str | None:
        """Record the durable outcome for ``pr_url``, once. ``ON CONFLICT
        (pr_url) DO NOTHING`` -- this is detected once (pr_outcomes.py polls
        each PR's real GitHub state exactly until its first closed/merged
        observation) and never rewritten, so a later, unrelated call for the
        same PR can't clobber the original evidence. Returns the new row's
        id, or ``None`` when a row for this ``pr_url`` already existed.
        """
        outcome_id = uuid.uuid4().hex
        row = await self._pool.fetchrow(
            """
            INSERT INTO pr_outcomes
                (id, pr_url, assessment_id, app_name, category, finding_category,
                 skill_names_json, outcome, reject_reason, edit_diff_json, detected_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10::jsonb, $11)
            ON CONFLICT (pr_url) DO NOTHING
            RETURNING id
            """,
            outcome_id, pr_url, assessment_id, app_name, category, finding_category,
            json.dumps(skill_names or []), outcome, reject_reason,
            json.dumps(edit_diff or []), _now(),
        )
        return row["id"] if row else None

    async def get_pr_outcome(self, pr_url: str) -> dict | None:
        row = await self._pool.fetchrow("SELECT * FROM pr_outcomes WHERE pr_url = $1", pr_url)
        return _pr_outcome_row_to_dict(row)

    async def pr_outcomes_recorded_for(self, pr_urls: list[str]) -> set[str]:
        """Which of ``pr_urls`` already has a recorded outcome -- one batched
        query so a caller checking many PRs at once (pr_outcomes.py's sync
        pass) never re-runs the real GitHub detection calls for a PR it's
        already durably recorded."""
        if not pr_urls:
            return set()
        rows = await self._pool.fetch(
            "SELECT pr_url FROM pr_outcomes WHERE pr_url = ANY($1::text[])", pr_urls,
        )
        return {r["pr_url"] for r in rows}

    async def get_pr_outcomes_for_urls(self, pr_urls: list[str]) -> dict[str, dict]:
        """Batched ``{pr_url: outcome_dict}`` for every one of ``pr_urls``
        that has a recorded outcome -- one query so a caller attaching
        outcomes onto many PR records at once (pr_tracking.py's
        ``attach_pr_outcomes()``) never issues one query per record."""
        if not pr_urls:
            return {}
        rows = await self._pool.fetch(
            "SELECT * FROM pr_outcomes WHERE pr_url = ANY($1::text[])", pr_urls,
        )
        return {d["pr_url"]: d for r in rows if (d := _pr_outcome_row_to_dict(r)) is not None}

    async def get_human_override(self, app_name: str, finding_category: str) -> str | None:
        """Get the most recent human override value for this app/category."""
        row = await self._pool.fetchrow(
            """SELECT human_value FROM agent_feedback
               WHERE app_name = $1 AND finding_category = $2 AND action = 'modified' AND human_value != ''
               ORDER BY created_at DESC LIMIT 1""",
            app_name, finding_category,
        )
        return row["human_value"] if row else None
