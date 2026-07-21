"""``AgentsMixin`` -- the ``agent_registry`` table (which agents/watchers
exist and their liveness heartbeat) and the ``agent_runs`` table (a
structured record of every individual agent execution), plus the
aggregate stats view derived from the latter.

Grouped together because ``get_agent_stats()`` (originally filed under the
old file's "Trust / Transparency" section header) is really just a
``GROUP BY agent_name`` aggregation over ``agent_runs`` -- the same table
``save_agent_run()``/``list_agent_runs()`` already own -- not an
independent concern of its own.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import uuid

import asyncpg

from ._shared import _now, _rows_to_dicts


class AgentsMixin:
    _pool: asyncpg.Pool

    async def register_agent(
        self, agent_name: str, category: str, capabilities: str = "[]"
    ) -> str:
        agent_id = uuid.uuid4().hex
        now = _now()
        row = await self._pool.fetchrow(
            """
            INSERT INTO agent_registry
                (id, agent_name, category, status, capabilities, last_heartbeat, registered_at)
            VALUES ($1, $2, $3, 'active', $4, $5, $6)
            ON CONFLICT (agent_name) DO UPDATE SET
                category = EXCLUDED.category,
                status = 'active',
                capabilities = EXCLUDED.capabilities,
                last_heartbeat = EXCLUDED.last_heartbeat
            RETURNING id
            """,
            agent_id, agent_name, category, capabilities, now, now,
        )
        return row["id"]

    async def list_agents(self, status: str = "active") -> list[dict]:
        """List registered agents, filtered by ``status``.

        In practice every row in ``agent_registry`` is always ``'active'``:
        both writers (``register_agent()``/``agent_heartbeat()`` above)
        hardcode ``status = 'active'`` in their own SQL, and
        ``prune_stale_agents()`` hard-deletes a stale row rather than
        marking it inactive -- there is currently no code path that can
        ever write any other status. The parameter is kept (rather than
        removed) since it costs nothing and documents the schema's intent
        for a future status this table doesn't use yet.
        """
        rows = await self._pool.fetch(
            "SELECT * FROM agent_registry WHERE status = $1 ORDER BY agent_name", status,
        )
        return _rows_to_dicts(rows)

    async def agent_heartbeat(self, agent_name: str, category: str = "watcher") -> bool:
        """Record a liveness heartbeat for an agent.

        Upserts: long-lived watchers (vuln-watcher, slo-tracker, drift-detector,
        skill-learner) never go through ``register_agent`` the way onboarding
        agents do, so without this an UPDATE against a non-existent row would
        silently no-op and the Agents/Schedules pages would never show a real
        "last seen" for them.
        """
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO agent_registry
                (id, agent_name, category, status, capabilities, last_heartbeat, registered_at)
            VALUES ($1, $2, $3, 'active', '[]', $4, $4)
            ON CONFLICT (agent_name) DO UPDATE SET last_heartbeat = EXCLUDED.last_heartbeat
            """,
            uuid.uuid4().hex, agent_name, category, now,
        )
        return True

    async def prune_stale_agents(self, known_names: frozenset[str] | set[str]) -> list[str]:
        """Delete `agent_registry` rows for agent names outside `known_names`."""
        rows = await self._pool.fetch("SELECT DISTINCT agent_name FROM agent_registry")
        stale = sorted(r["agent_name"] for r in rows if r["agent_name"] not in known_names)
        if stale:
            await self._pool.execute(
                "DELETE FROM agent_registry WHERE agent_name = ANY($1::text[])", stale,
            )
        return stale

    async def get_agent_stats(self, agent_name: str = "") -> list[dict]:
        """Get performance stats per agent from structured `agent_runs` records.

        Mirrored row-for-row against ``agent_runs`` rather than LIKE-matching
        event `action` strings over the raw `events` table (that heuristic
        double-counted unrelated actions like 'onboarding-complete' and
        undercounted agents whose events don't follow that naming
        convention).
        """
        query = """
            SELECT agent_name,
                   COUNT(*) as total_runs,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) as failures,
                   AVG(duration_ms) as avg_duration_ms,
                   MIN(started_at) as first_seen,
                   MAX(started_at) as last_seen
            FROM agent_runs
        """
        params: list[str] = []
        if agent_name:
            params.append(agent_name)
            query += " WHERE agent_name = $1"
        query += " GROUP BY agent_name ORDER BY total_runs DESC"
        rows = await self._pool.fetch(query, *params)
        stats = []
        for r in rows:
            total = r["total_runs"] or 0
            success_rate = (r["successes"] / total * 100) if total > 0 else 0
            stats.append({
                "agent": r["agent_name"],
                "total_events": total,
                "successes": r["successes"],
                "failures": r["failures"],
                "success_rate": round(success_rate, 1),
                "avg_duration_ms": round(r["avg_duration_ms"]) if r["avg_duration_ms"] is not None else None,
                "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            })
        return stats

    async def save_agent_run(
        self,
        agent_name: str,
        mode: str,
        status: str,
        assessment_id: str | None = None,
        duration_ms: int | None = None,
        resource_tier: str | None = None,
        error: str | None = None,
    ) -> str:
        """Record a single structured agent execution (one row per run)."""
        run_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO agent_runs
                (id, assessment_id, agent_name, mode, status, duration_ms, resource_tier, error, started_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            run_id, assessment_id, agent_name, mode, status,
            duration_ms, resource_tier, error, _now(),
        )
        return run_id

    async def list_agent_runs(self, agent_name: str, limit: int = 50) -> list[dict]:
        """Real per-run history for an agent, most recent first."""
        rows = await self._pool.fetch(
            "SELECT * FROM agent_runs WHERE agent_name = $1 ORDER BY started_at DESC LIMIT $2",
            agent_name, limit,
        )
        return _rows_to_dicts(rows)
