"""``SkillsMixin`` -- the ``skill_effectiveness`` table (an approve/reject
outcome recorded every time a generated skill's output was accepted or
declined) and the derived self-improvement-loop health views built on top
of it, plus the ``skill_inventory_snapshots`` table (point-in-time
snapshots of the skills/checks catalog, for "did anything change?"
tracking independent of ``git log``).

Grouped together since ``get_loop_health()``/``get_skill_history()`` (loop
health, per-skill lifecycle) are themselves built directly on
``get_skill_effectiveness()``/``get_low_effectiveness_skills()`` in this
same mixin -- one cohesive "how well is a skill doing, and is the loop
learning from it" concern. ``skill_inventory_snapshots`` is a smaller,
adjacent concern (the catalog itself, not any one skill's outcomes) kept
in the same module rather than split out for one 2-method table.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import asyncpg

from ._shared import _now, _recency_weight, _rows_to_dicts


class SkillsMixin:
    _pool: asyncpg.Pool

    async def record_skill_outcome(self, skill_name: str, app_name: str, outcome: str, reason: str = '') -> None:
        await self._pool.execute(
            'INSERT INTO skill_effectiveness (skill_name, app_name, outcome, reason, created_at) VALUES ($1, $2, $3, $4, $5)',
            skill_name, app_name, outcome, reason, _now(),
        )

    async def get_skill_effectiveness(
        self, skill_name: str = '', min_count: int = 5, half_life_days: float = 90.0,
    ) -> dict:
        """Per-skill outcome tallies, plus a recency-weighted approval rate.

        Fetches raw ``created_at`` per row and weights in Python (rather
        than aggregated in SQL) so the weighting logic stays simple and
        auditable.
        """
        if skill_name:
            rows = await self._pool.fetch(
                'SELECT skill_name, outcome, created_at FROM skill_effectiveness WHERE skill_name = $1',
                skill_name,
            )
        else:
            rows = await self._pool.fetch(
                'SELECT skill_name, outcome, created_at FROM skill_effectiveness',
            )

        now = datetime.now(timezone.utc)
        stats: dict[str, dict] = {}
        for r in rows:
            name = r['skill_name']
            if name not in stats:
                stats[name] = {'approved': 0, 'rejected': 0, 'total': 0,
                                '_weighted_approved': 0.0, '_weighted_total': 0.0}
            outcome = r['outcome']
            stats[name][outcome] = stats[name].get(outcome, 0) + 1
            stats[name]['total'] += 1

            weight = _recency_weight(r['created_at'], now, half_life_days)
            stats[name]['_weighted_total'] += weight
            if outcome == 'approved':
                stats[name]['_weighted_approved'] += weight

        result: dict[str, dict] = {}
        for name, s in stats.items():
            if s['total'] < min_count:
                continue
            weighted_total = s.pop('_weighted_total')
            weighted_approved = s.pop('_weighted_approved')
            s['weighted_rate'] = weighted_approved / weighted_total if weighted_total > 0 else 0.0
            result[name] = s
        return result

    async def get_low_effectiveness_skills(self, min_count: int = 5, max_rate: float = 0.3) -> list[dict]:
        """Skills flagged for review by their recency-weighted approval rate."""
        stats = await self.get_skill_effectiveness(min_count=min_count)
        low: list[dict] = []
        for name, s in stats.items():
            rate = s['weighted_rate']
            if rate < max_rate:
                raw_rate = s['approved'] / s['total'] if s['total'] > 0 else 0
                low.append({
                    'skill': name,
                    'approval_rate': round(rate, 2),
                    'raw_approval_rate': round(raw_rate, 2),
                    'total': s['total'],
                })
        return low

    async def get_recent_skill_activity(self, limit: int = 20) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT skill_name, app_name, outcome, reason, created_at "
            "FROM skill_effectiveness ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return _rows_to_dicts(rows)

    async def get_loop_health(self, window_days: int = 30) -> dict:
        """Self-improvement loop health."""
        flagged = await self.get_low_effectiveness_skills()
        if not flagged:
            return {"flagged_count": 0, "with_recent_improvement": 0,
                    "pct_with_improvement": None, "window_days": window_days}

        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        with_improvement = 0
        for entry in flagged:
            row = await self._pool.fetchrow(
                "SELECT 1 FROM events WHERE action = 'skill-improvement-drafted' "
                "AND summary LIKE $1 AND timestamp >= $2 LIMIT 1",
                f"%{entry['skill']}%", cutoff,
            )
            if row is not None:
                with_improvement += 1

        return {
            "flagged_count": len(flagged),
            "with_recent_improvement": with_improvement,
            "pct_with_improvement": round(with_improvement / len(flagged) * 100, 1),
            "window_days": window_days,
        }

    async def get_skill_history(self, skill_name: str, limit: int = 50) -> dict:
        """Per-skill lifecycle view."""
        outcomes = await self._pool.fetch(
            "SELECT app_name, outcome, reason, created_at FROM skill_effectiveness "
            "WHERE skill_name = $1 ORDER BY created_at DESC LIMIT $2",
            skill_name, limit,
        )
        events = await self._pool.fetch(
            "SELECT timestamp, agent_id, action, severity, summary FROM events "
            "WHERE summary LIKE $1 ORDER BY timestamp DESC LIMIT $2",
            f"%{skill_name}%", limit,
        )
        return {
            "outcomes": _rows_to_dicts(outcomes),
            "events": _rows_to_dicts(events),
        }

    # ── Skill/Check Inventory Snapshots ─────────────────────────────────
    #
    # Tracks additions/removals to the skills/checks catalog over time so
    # the "did anything change?" question has an in-app answer beyond
    # `git log skills/ checks/`. See agentit.skill_inventory for the
    # snapshot/diff logic that produces the JSON blob stored here.

    async def save_skill_inventory_snapshot(self, snapshot_json: dict) -> str:
        """Persist a skill/check inventory snapshot (as a JSON-serializable dict)."""
        row = await self._pool.fetchrow(
            "INSERT INTO skill_inventory_snapshots (snapshot_json, created_at) VALUES ($1::jsonb, $2) RETURNING id",
            json.dumps(snapshot_json), _now(),
        )
        return str(row["id"])

    async def get_last_skill_inventory_snapshot(self) -> dict | None:
        """Return the most recently saved snapshot dict, or ``None`` if none exists yet."""
        row = await self._pool.fetchrow(
            "SELECT snapshot_json, created_at FROM skill_inventory_snapshots "
            "ORDER BY id DESC LIMIT 1",
        )
        if row is None:
            return None
        data = json.loads(row["snapshot_json"])
        data["created_at"] = row["created_at"].isoformat()
        return data
