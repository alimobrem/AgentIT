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
from collections import Counter
from datetime import datetime, timedelta, timezone

import asyncpg

from ._shared import _now, _recency_weight, _rows_to_dicts

# After this many rejects with the same reason prefix for (app, skill),
# SkillEngine skips that skill for the app (clear-evidence theater /
# still-present loops). Kept at 2 so the second identical failure cools
# the skill instead of regenerating the same fix again.
SKILL_COOLDOWN_SAME_REASON = 2

# skill-learner fast-path: aligned with SKILL_COOLDOWN_SAME_REASON so a
# single-app cool-down still flags the skill for improvement research
# (otherwise regenerate stops at 2 and the learner never sees a 3rd row).
SKILL_LEARNER_IDENTICAL_REJECT_THRESHOLD = SKILL_COOLDOWN_SAME_REASON

_STABLE_REJECT_PREFIXES = (
    "finding still present after merge",
    "finding cleared after merge",
    "clear-evidence",
    "pr closed without merge",
)


def skill_reject_reason_prefix(reason: str, max_len: int = 64) -> str:
    """Normalize a skill_effectiveness.reason into a cool-down key."""
    text = (reason or "").strip().lower()
    if not text:
        return ""
    for stable in _STABLE_REJECT_PREFIXES:
        if text.startswith(stable) or stable in text:
            return stable
    for sep in (" — ", " - ", ": ", " ("):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    return text[:max_len]



class SkillsMixin:
    _pool: asyncpg.Pool

    async def record_skill_outcome(self, skill_name: str, app_name: str, outcome: str, reason: str = '') -> None:
        await self._pool.execute(
            'INSERT INTO skill_effectiveness (skill_name, app_name, outcome, reason, created_at) VALUES ($1, $2, $3, $4, $5)',
            skill_name, app_name, outcome, reason, _now(),
        )

    async def get_recent_skill_reject_reasons(
        self, app_name: str, skill_name: str, *, limit: int = 3,
    ) -> list[str]:
        """Most recent reject reasons for ``(app, skill)``, newest first."""
        rows = await self._pool.fetch(
            "SELECT reason FROM skill_effectiveness "
            "WHERE app_name = $1 AND skill_name = $2 AND outcome = 'rejected' "
            "ORDER BY created_at DESC LIMIT $3",
            app_name, skill_name, limit,
        )
        return [(r["reason"] or "") for r in rows]

    async def count_skill_rejects_with_prefix(
        self, app_name: str, skill_name: str, reason_prefix: str,
    ) -> int:
        """How many rejects for ``(app, skill)`` share ``reason_prefix``."""
        prefix = skill_reject_reason_prefix(reason_prefix)
        if not prefix:
            return 0
        rows = await self._pool.fetch(
            "SELECT reason FROM skill_effectiveness "
            "WHERE app_name = $1 AND skill_name = $2 AND outcome = 'rejected'",
            app_name, skill_name,
        )
        return sum(
            1 for r in rows
            if skill_reject_reason_prefix(r["reason"] or "") == prefix
        )

    async def is_skill_cooling_down(
        self,
        app_name: str,
        skill_name: str,
        *,
        threshold: int = SKILL_COOLDOWN_SAME_REASON,
    ) -> bool:
        """True when ``(app, skill)`` has ``threshold``+ rejects with one prefix."""
        info = await self.get_skill_cooldown(app_name, skill_name, threshold=threshold)
        return info is not None

    async def get_skill_cooldown(
        self,
        app_name: str,
        skill_name: str,
        *,
        threshold: int = SKILL_COOLDOWN_SAME_REASON,
    ) -> dict | None:
        """Cool-down info for one skill on one app, or ``None`` if active."""
        rows = await self._pool.fetch(
            "SELECT reason FROM skill_effectiveness "
            "WHERE app_name = $1 AND skill_name = $2 AND outcome = 'rejected'",
            app_name, skill_name,
        )
        counts: Counter[str] = Counter()
        for r in rows:
            prefix = skill_reject_reason_prefix(r["reason"] or "")
            if prefix:
                counts[prefix] += 1
        if not counts:
            return None
        top_prefix, top_count = counts.most_common(1)[0]
        if top_count < threshold:
            return None
        return {
            "skill_name": skill_name,
            "app_name": app_name,
            "reason_prefix": top_prefix,
            "count": top_count,
            "cooling_down": True,
        }

    async def list_cooled_skills(
        self,
        app_name: str = "",
        *,
        threshold: int = SKILL_COOLDOWN_SAME_REASON,
    ) -> list[dict]:
        """Skills cooling down (optionally scoped to one app)."""
        if app_name:
            rows = await self._pool.fetch(
                "SELECT skill_name, reason FROM skill_effectiveness "
                "WHERE app_name = $1 AND outcome = 'rejected'",
                app_name,
            )
        else:
            rows = await self._pool.fetch(
                "SELECT skill_name, app_name, reason FROM skill_effectiveness "
                "WHERE outcome = 'rejected'",
            )
        grouped: dict[tuple[str, str], Counter[str]] = {}
        for r in rows:
            skill = r["skill_name"]
            app = app_name or r["app_name"]
            prefix = skill_reject_reason_prefix(r["reason"] or "")
            if not prefix:
                continue
            grouped.setdefault((app, skill), Counter())[prefix] += 1
        cooled: list[dict] = []
        for (app, skill), counts in grouped.items():
            top_prefix, top_count = counts.most_common(1)[0]
            if top_count >= threshold:
                cooled.append({
                    "skill_name": skill,
                    "app_name": app,
                    "reason_prefix": top_prefix,
                    "count": top_count,
                    "cooling_down": True,
                })
        cooled.sort(key=lambda x: (-x["count"], x["skill_name"], x["app_name"]))
        return cooled

    async def get_skills_with_identical_reject_reasons(
        self,
        *,
        min_identical: int = SKILL_LEARNER_IDENTICAL_REJECT_THRESHOLD,
    ) -> list[dict]:
        """Skills with ``min_identical``+ rejects sharing one reason prefix."""
        rows = await self._pool.fetch(
            "SELECT skill_name, reason, outcome FROM skill_effectiveness",
        )
        by_skill: dict[str, Counter[str]] = {}
        totals: dict[str, dict[str, int]] = {}
        for r in rows:
            name = r["skill_name"]
            totals.setdefault(name, {"approved": 0, "rejected": 0, "total": 0})
            outcome = r["outcome"]
            totals[name][outcome] = totals[name].get(outcome, 0) + 1
            totals[name]["total"] += 1
            if outcome != "rejected":
                continue
            prefix = skill_reject_reason_prefix(r["reason"] or "")
            if prefix:
                by_skill.setdefault(name, Counter())[prefix] += 1
        flagged: list[dict] = []
        for name, counts in by_skill.items():
            top_prefix, top_count = counts.most_common(1)[0]
            if top_count < min_identical:
                continue
            t = totals[name]
            approved = t.get("approved", 0)
            total = t.get("total", 0) or 1
            flagged.append({
                "skill": name,
                "approval_rate": round(approved / total, 2),
                "raw_approval_rate": round(approved / total, 2),
                "total": total,
                "identical_reject_prefix": top_prefix,
                "identical_reject_count": top_count,
                "fast_path": True,
            })
        flagged.sort(key=lambda x: (-x["identical_reject_count"], x["skill"]))
        return flagged

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
