"""Async, Postgres-backed ``AssessmentStore`` -- the only supported store.

Postgres is not a backend option among several; it is the store. SQLite
support (the original prototype backend) and the backend-selection
machinery that briefly coexisted with it (``store_pg.py``, ``store_factory.py``,
``AGENTIT_DB_BACKEND``) have been removed -- see ``docs/postgres-migration-plan.md``
for the full history of how this cutover happened and why (that doc is now
marked historically-complete/superseded, not a live plan).

This class is raw-SQL (no ORM), one hand-written ``SELECT``/``INSERT``/
``UPDATE`` per method, using ``asyncpg`` for the async Postgres driver.
Construct via the ``create()`` classmethod (pool creation is inherently
async, so a plain ``__init__`` can't do it) -- ``dsn`` defaults to the
``AGENTIT_DB_DSN`` environment variable if not passed explicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from agentit.models import AssessmentReport, Severity

logger = logging.getLogger(__name__)


def _recency_weight(created_at: Any, now: datetime, half_life_days: float) -> float:
    """Exponential recency weight for a ``skill_effectiveness`` row: 1.0 for
    an outcome recorded right now, 0.5 at ``half_life_days`` old, 0.25 at
    twice that, etc. Accepts either a native ``datetime`` (what asyncpg
    returns for a ``TIMESTAMPTZ`` column) or an ISO-8601 string, so this
    stays usable regardless of whether a row came straight from ``asyncpg``
    or from a dict already normalized by ``_row_to_dict``. Malformed/missing
    timestamps fall back to full weight (1.0) rather than dropping the row
    -- an outcome with an unparsable timestamp is still a real outcome, just
    one this can't age-discount.
    """
    if isinstance(created_at, datetime):
        recorded_at = created_at
    elif isinstance(created_at, str):
        try:
            recorded_at = datetime.fromisoformat(created_at)
        except ValueError:
            return 1.0
    else:
        return 1.0
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    age_days = max((now - recorded_at).total_seconds() / 86400.0, 0.0)
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


# Idempotent DDL for all 19 tables. Run once (see `AssessmentStore.create`)
# via a single multi-statement `execute()` call — asyncpg uses the simple
# query protocol (which permits multiple `;`-separated statements) whenever
# `execute()` is called with no bind parameters.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    assessed_at TIMESTAMPTZ NOT NULL,
    criticality TEXT NOT NULL,
    overall_score DOUBLE PRECISION NOT NULL,
    report_json JSONB NOT NULL
);

-- `apps`: one row per unique `repo_url`, holding facts that are genuinely
-- properties of the APP (persist across every assessment of it over time)
-- rather than of any one assessment RUN -- see docs/architecture.md's
-- "Data model: assessments vs. apps" section.
CREATE TABLE IF NOT EXISTS apps (
    repo_url TEXT PRIMARY KEY,
    repo_name TEXT NOT NULL,
    infra_repo_url TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_results (
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id),
    created_at TIMESTAMPTZ NOT NULL,
    files_json JSONB NOT NULL,
    orchestration_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    pr_url TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_app TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    summary TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);
CREATE INDEX IF NOT EXISTS idx_events_correlation_id ON events(correlation_id);

CREATE TABLE IF NOT EXISTS gates (
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id),
    gate_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS remediations (
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id),
    agent_name TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_registry (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    -- TEXT, not JSONB: despite the name, callers (AGENT_CAPABILITIES in
    -- agents/capabilities.py) pass a human-readable prose description
    -- ("VPA, cost labels, cost report"), never an actual JSON value, and
    -- routes/capabilities.py reads it back as a plain string too.
    capabilities TEXT NOT NULL DEFAULT '[]',
    last_heartbeat TIMESTAMPTZ,
    registered_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS slos (
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id),
    metric_name TEXT NOT NULL,
    target_value DOUBLE PRECISION NOT NULL,
    current_value DOUBLE PRECISION,
    status TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS apply_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id),
    namespace TEXT NOT NULL,
    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
    applied_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    skipped_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    errors_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    repo_files_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS remediation_jobs (
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT NOT NULL DEFAULT '',
    steps_completed JSONB NOT NULL DEFAULT '[]'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_operations (
    id TEXT PRIMARY KEY,
    app_name TEXT NOT NULL,
    job_name TEXT NOT NULL,
    agent TEXT NOT NULL,
    schedule TEXT NOT NULL,
    command TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_webhooks (
    delivery_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_feedback (
    id TEXT PRIMARY KEY,
    app_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    finding_category TEXT NOT NULL,
    action TEXT NOT NULL,
    human_reason TEXT,
    original_value TEXT,
    human_value TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_effectiveness (
    skill_name TEXT NOT NULL,
    app_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (skill_name, app_name, created_at)
);

CREATE TABLE IF NOT EXISTS suppressed_checks (
    id TEXT PRIMARY KEY,
    app_name TEXT NOT NULL,
    check_source TEXT NOT NULL,
    reason TEXT,
    suppressed_by TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(app_name, check_source)
);

CREATE TABLE IF NOT EXISTS skill_inventory_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- Structured per-run agent records — replaces the fragile action-string
-- heuristic previously used by get_agent_stats().
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    assessment_id TEXT,
    agent_name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL,
    duration_ms INTEGER,
    resource_tier TEXT,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_assessment ON agent_runs(assessment_id);

-- Per-check pass/fail snapshots, keyed by assessment, for fleet-wide check
-- compliance reporting.
CREATE TABLE IF NOT EXISTS check_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    dimension TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_results_assessment ON check_results(assessment_id);

-- Tracks every change set routed through the unified delivery flow
-- (portal/delivery.py::route_and_deliver). See docs/unified-apply-flow.md
-- section (C).
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL REFERENCES assessments(id),
    app_name TEXT NOT NULL,
    categories_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    mechanism TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    verification TEXT NOT NULL DEFAULT 'unknown',
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_assessment ON deliveries(assessment_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_app ON deliveries(app_name);

-- One-time backfill for databases that predate the `apps` table: populate
-- it from existing `assessments` history using "most recent non-null value
-- wins". `WHERE repo_url NOT IN (SELECT repo_url FROM apps)` makes this
-- cheap in steady state (once every app has a row, the correlated
-- subquery below never runs again) and `ON CONFLICT DO NOTHING` makes it
-- safe against concurrent replicas (portal x2 + 4 watchers all call
-- `create()`, and therefore run this exact statement, at startup) without
-- ever overwriting a row `save()`/`set_infra_repo_url()` already wrote.
INSERT INTO apps (repo_url, repo_name, infra_repo_url, created_at, updated_at)
SELECT
    latest.repo_url,
    latest.repo_name,
    (
        SELECT a2.report_json->>'infra_repo_url'
        FROM assessments a2
        WHERE a2.repo_url = latest.repo_url
          AND a2.report_json->>'infra_repo_url' IS NOT NULL
        ORDER BY a2.assessed_at DESC
        LIMIT 1
    ),
    latest.assessed_at,
    latest.assessed_at
FROM (
    SELECT DISTINCT ON (repo_url) repo_url, repo_name, assessed_at
    FROM assessments
    WHERE repo_url NOT IN (SELECT repo_url FROM apps)
    ORDER BY repo_url, assessed_at DESC
) latest
ON CONFLICT (repo_url) DO NOTHING;
"""

_ALL_TABLES = [
    "assessments", "apps", "onboarding_results", "events", "gates",
    "remediations", "agent_registry", "slos", "apply_results",
    "settings", "remediation_jobs", "scheduled_operations",
    "processed_webhooks", "agent_feedback", "skill_effectiveness",
    "suppressed_checks", "skill_inventory_snapshots",
    "agent_runs", "check_results", "deliveries",
]


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    """Convert an asyncpg Record to a dict, normalizing datetimes to
    ISO-8601 strings so callers get a stable, JSON-serializable shape
    regardless of column type."""
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


def _rows_to_dicts(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def _delivery_row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    d = _row_to_dict(row)
    if d is None:
        return None
    d["categories"] = json.loads(d.pop("categories_json"))
    d["details"] = json.loads(d.pop("details_json"))
    return d


def _affected(status: str) -> int:
    """Parse the affected-row count out of an asyncpg command-tag string
    such as ``"UPDATE 1"`` or ``"DELETE 3"``."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssessmentStore:
    """The one and only ``AssessmentStore``. Postgres-backed, fully async.

    Construct via the ``create()`` classmethod (pool creation is inherently
    async, so a plain ``__init__`` can't do it).
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(
        cls,
        dsn: str | None = None,
        *,
        min_size: int = 5,
        max_size: int = 20,
    ) -> "AssessmentStore":
        if dsn is None:
            dsn = os.environ.get("AGENTIT_DB_DSN")
        if not dsn:
            raise ValueError(
                "No Postgres DSN provided and AGENTIT_DB_DSN is not set."
            )
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        await pool.execute(SCHEMA_SQL)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def _refresh_active_gates_metric(self) -> None:
        """Keep the `agentit_active_gates` gauge in sync with pending gate count.

        Called from every method that creates/resolves/expires a gate so the
        gauge is correct regardless of which caller (portal route, automode,
        slo-tracker, ...) triggered the change.
        """
        try:
            from agentit.portal.metrics import active_gates
            count = await self._pool.fetchval("SELECT COUNT(*) FROM gates WHERE status = 'pending'")
            active_gates.set(count or 0)
        except Exception:
            logger.debug("Failed to refresh active_gates gauge", exc_info=True)

    # ── Settings ───────────────────────────────────────────────────────

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

    async def save_apply_results(
        self, assessment_id: str, results: dict, namespace: str, dry_run: bool,
    ) -> None:
        now = _now()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO apply_results
                       (assessment_id, namespace, dry_run, applied_json, skipped_json, errors_json, repo_files_json, created_at)
                       VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8)""",
                    assessment_id, namespace, dry_run,
                    json.dumps(results["applied"]),
                    json.dumps(results["skipped"]),
                    json.dumps(results["errors"]),
                    json.dumps(results.get("repo_files", [])),
                    now,
                )
                if results.get("missing_operators"):
                    await conn.execute(
                        "DELETE FROM apply_results WHERE assessment_id = $1 AND created_at < $2",
                        assessment_id, now,
                    )

    async def get_apply_results(self, assessment_id: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM apply_results WHERE assessment_id = $1 ORDER BY created_at DESC LIMIT 1",
            assessment_id,
        )
        if row is None:
            return None
        return {
            "namespace": row["namespace"],
            "dry_run": bool(row["dry_run"]),
            "applied": json.loads(row["applied_json"]),
            "skipped": json.loads(row["skipped_json"]),
            "errors": json.loads(row["errors_json"]),
            "repo_files": json.loads(row["repo_files_json"]),
            "created_at": row["created_at"].isoformat(),
        }

    async def _upsert_app(self, repo_url: str, repo_name: str, infra_repo_url: str | None) -> None:
        """Upsert the app-level facts row -- see docs/architecture.md's
        "Data model: assessments vs. apps" section for the full rationale.
        """
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO apps (repo_url, repo_name, infra_repo_url, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $4)
            ON CONFLICT (repo_url) DO UPDATE SET
                repo_name = EXCLUDED.repo_name,
                infra_repo_url = COALESCE(EXCLUDED.infra_repo_url, apps.infra_repo_url),
                updated_at = EXCLUDED.updated_at
            """,
            repo_url, repo_name, infra_repo_url, now,
        )

    async def _last_known_infra_repo_url(self, repo_url: str) -> str | None:
        """Reads the `apps` table's always-current ``infra_repo_url`` to
        carry a previously-set value forward across re-assessments of the
        same app."""
        row = await self._pool.fetchrow(
            "SELECT infra_repo_url FROM apps WHERE repo_url = $1", repo_url,
        )
        return row["infra_repo_url"] if row is not None else None

    async def save(self, report: AssessmentReport) -> str:
        assessment_id = uuid.uuid4().hex
        if report.infra_repo_url is None:
            report.infra_repo_url = await self._last_known_infra_repo_url(report.repo_url)
        await self._pool.execute(
            """
            INSERT INTO assessments (id, repo_url, repo_name, assessed_at, criticality, overall_score, report_json)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            assessment_id,
            report.repo_url,
            report.repo_name,
            report.assessed_at,
            report.criticality,
            report.overall_score,
            report.model_dump_json(),
        )
        await self._upsert_app(report.repo_url, report.repo_name, report.infra_repo_url)
        await self.log_event(
            "assessor",
            "assessment-complete",
            report.repo_name,
            "info",
            f"Assessment complete: {report.overall_score:.0f}/100",
            correlation_id=assessment_id,
        )
        return assessment_id

    async def get(self, assessment_id: str) -> AssessmentReport | None:
        row = await self._pool.fetchrow(
            "SELECT report_json FROM assessments WHERE id = $1", assessment_id,
        )
        if row is None:
            return None
        return AssessmentReport.model_validate_json(row["report_json"])

    async def set_infra_repo_url(self, assessment_id: str, infra_repo_url: str) -> bool:
        report = await self.get(assessment_id)
        if report is None:
            return False
        report.infra_repo_url = infra_repo_url
        result = await self._pool.execute(
            "UPDATE assessments SET report_json = $1 WHERE id = $2",
            report.model_dump_json(), assessment_id,
        )
        await self._upsert_app(report.repo_url, report.repo_name, infra_repo_url)
        return _affected(result) > 0

    async def list_all(self) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT id, repo_name, repo_url, assessed_at, overall_score, criticality
            FROM assessments ORDER BY assessed_at DESC
            """
        )
        return _rows_to_dicts(rows)

    async def delete(self, assessment_id: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM remediation_jobs WHERE assessment_id = $1", assessment_id)
                await conn.execute("DELETE FROM onboarding_results WHERE assessment_id = $1", assessment_id)
                await conn.execute("DELETE FROM remediations WHERE assessment_id = $1", assessment_id)
                await conn.execute("DELETE FROM slos WHERE assessment_id = $1", assessment_id)
                await conn.execute("DELETE FROM gates WHERE assessment_id = $1", assessment_id)
                await conn.execute("DELETE FROM apply_results WHERE assessment_id = $1", assessment_id)
                status = await conn.execute("DELETE FROM assessments WHERE id = $1", assessment_id)
        return _affected(status) > 0

    async def save_onboarding(
        self, assessment_id: str, files: list[dict], orchestration: dict | None = None,
    ) -> str:
        onboarding_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO onboarding_results (id, assessment_id, created_at, files_json, orchestration_json)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            """,
            onboarding_id,
            assessment_id,
            _now(),
            json.dumps(files),
            json.dumps(orchestration or {}),
        )
        row = await self._pool.fetchrow(
            "SELECT repo_name FROM assessments WHERE id = $1", assessment_id,
        )
        target_app = row["repo_name"] if row else assessment_id
        await self.log_event(
            "onboarding",
            "onboarding-complete",
            target_app,
            "info",
            f"Generated {len(files)} manifests",
            correlation_id=assessment_id,
        )
        return onboarding_id

    async def get_onboarding(self, assessment_id: str) -> list[dict] | None:
        row = await self._pool.fetchrow(
            "SELECT files_json FROM onboarding_results WHERE assessment_id = $1 ORDER BY created_at DESC LIMIT 1",
            assessment_id,
        )
        if row is None:
            return None
        return json.loads(row["files_json"])

    async def get_latest_onboarding(self, assessment_id: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM onboarding_results WHERE assessment_id = $1 ORDER BY created_at DESC LIMIT 1",
            assessment_id,
        )
        return _row_to_dict(row)

    async def get_orchestration(self, assessment_id: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT orchestration_json FROM onboarding_results WHERE assessment_id = $1 ORDER BY created_at DESC LIMIT 1",
            assessment_id,
        )
        if row is None:
            return None
        return json.loads(row["orchestration_json"])

    async def update_onboarding_file(
        self, assessment_id: str, category: str, path: str, content: str,
    ) -> dict | None:
        """Read-modify-write happens inside one ``asyncpg`` transaction so a
        concurrent edit of a different file can't race the ``files_json``
        read/write pair."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, files_json FROM onboarding_results WHERE assessment_id = $1 "
                    "ORDER BY created_at DESC LIMIT 1",
                    assessment_id,
                )
                if row is None:
                    return None
                files = json.loads(row["files_json"])
                target = next(
                    (f for f in files if f.get("category") == category and f.get("path") == path), None,
                )
                if target is None:
                    return None
                if "original_content" not in target:
                    target["original_content"] = target["content"]
                target["content"] = content
                target["edited"] = True
                target["edited_at"] = _now().isoformat()
                await conn.execute(
                    "UPDATE onboarding_results SET files_json = $1::jsonb WHERE id = $2",
                    json.dumps(files), row["id"],
                )
        return target

    async def update_pr_url(self, assessment_id: str, pr_url: str) -> None:
        await self._pool.execute(
            """
            UPDATE onboarding_results SET pr_url = $1
            WHERE id = (
                SELECT id FROM onboarding_results
                WHERE assessment_id = $2 ORDER BY created_at DESC LIMIT 1
            )
            """,
            pr_url, assessment_id,
        )

    async def list_onboardings(self, assessment_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT id, assessment_id, created_at, files_json, orchestration_json, pr_url
            FROM onboarding_results WHERE assessment_id = $1
            ORDER BY created_at DESC
            """,
            assessment_id,
        )
        result = []
        for r in rows:
            files = json.loads(r["files_json"])
            orch = json.loads(r["orchestration_json"]) if r["orchestration_json"] else {}
            categories = list({f["category"] for f in files})
            result.append({
                "id": r["id"],
                "created_at": r["created_at"].isoformat(),
                "file_count": len(files),
                "categories": categories,
                "recommendation": orch.get("recommendation", ""),
                "auto_approve": orch.get("auto_approve", False),
                "pr_url": r["pr_url"] or "",
            })
        return result

    # ── Events ──────────────────────────────────────────────────────────

    async def log_event(
        self,
        agent_id: str,
        action: str,
        target_app: str | None,
        severity: str,
        summary: str,
        details: dict | None = None,
        correlation_id: str | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO events (id, timestamp, agent_id, action, target_app, severity, summary, details_json, correlation_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            """,
            event_id,
            _now(),
            agent_id,
            action,
            target_app,
            severity,
            summary,
            json.dumps(details or {}),
            correlation_id,
        )
        return event_id

    async def list_events(
        self, limit: int = 50, target_app: str | None = None
    ) -> list[dict]:
        if target_app is not None:
            rows = await self._pool.fetch(
                "SELECT * FROM events WHERE target_app = $1 ORDER BY timestamp DESC LIMIT $2",
                target_app, limit,
            )
        else:
            rows = await self._pool.fetch(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT $1", limit,
            )
        return _rows_to_dicts(rows)

    async def list_events_by_agent(self, agent_id: str, limit: int = 50) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM events WHERE agent_id = $1 ORDER BY timestamp DESC LIMIT $2",
            agent_id, limit,
        )
        return _rows_to_dicts(rows)

    async def list_events_by_action(self, action: str, limit: int = 50) -> list[dict]:
        """Look up events by `action` rather than `agent_id`.

        Used for decision points (e.g. auto-mode's 'decision' action) whose
        `agent_id` varies by caller — the action name is the stable identity,
        not the agent_id, which may or may not carry real agent/skill attribution.
        """
        rows = await self._pool.fetch(
            "SELECT * FROM events WHERE action = $1 ORDER BY timestamp DESC LIMIT $2",
            action, limit,
        )
        return _rows_to_dicts(rows)

    async def get_event(self, event_id: str) -> dict | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM events WHERE id = $1", event_id,
        )
        return _row_to_dict(row)

    async def list_events_by_correlation_id(self, correlation_id: str, limit: int = 200) -> list[dict]:
        """Trace a single assess -> onboard -> apply chain end to end."""
        rows = await self._pool.fetch(
            "SELECT * FROM events WHERE correlation_id = $1 ORDER BY timestamp ASC LIMIT $2",
            correlation_id, limit,
        )
        return _rows_to_dicts(rows)

    async def list_dlq_messages(self, limit: int = 200) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM events WHERE action = 'dead-letter' ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
        return _rows_to_dicts(rows)

    async def _update_dlq(self, event_id: str, new_action: str) -> bool:
        status = await self._pool.execute(
            "UPDATE events SET action = $1 WHERE id = $2 AND action = 'dead-letter'",
            new_action, event_id,
        )
        return _affected(status) > 0

    async def retry_dlq_message(self, event_id: str) -> bool:
        """Republish a dead-lettered message to its original Kafka topic, then relabel the row.

        Falls back to a relabel-only retry (with a warning in the log summary)
        if the dead-letter event has no ``original_topic``/``original_message``
        recorded (e.g. rows written before this was tracked) or if Kafka is
        unavailable — the row is still marked retried either way so the
        operator sees the outcome rather than a silent no-op.
        """
        row = await self._pool.fetchrow(
            "SELECT * FROM events WHERE id = $1 AND action = 'dead-letter'", event_id,
        )
        if row is None:
            return False

        details = json.loads(row["details_json"] or "{}")
        original_topic = details.get("original_topic")
        original_message = details.get("original_message")

        republished = False
        if original_topic and isinstance(original_message, dict):
            try:
                from agentit.events import get_publisher

                result = original_message.get("result") or {}
                # EventPublisher.publish is a synchronous Kafka client call
                # (kafka-python has no async API) — bridge it onto a worker
                # thread so it doesn't block the event loop.
                await asyncio.to_thread(
                    get_publisher().publish,
                    original_topic,
                    agent_id=original_message.get("agentId", "dlq-retry"),
                    action=original_message.get("action", "retry"),
                    target_app=original_message.get("targetApp"),
                    severity=original_message.get("severity", "info"),
                    summary=result.get("summary", "") if isinstance(result, dict) else "",
                    details=result.get("details") if isinstance(result, dict) else None,
                    correlation_id=original_message.get("correlationId"),
                )
                republished = True
            except Exception:
                logger.exception("Failed to republish dead-letter event %s", event_id)

        await self._update_dlq(event_id, 'dlq-retry')
        summary = (
            f'Retried dead-letter event {event_id} (republished to {original_topic})'
            if republished
            else f'Retried dead-letter event {event_id} (relabelled only — republish unavailable)'
        )
        await self.log_event('portal', 'dlq-retry', row["target_app"], 'info', summary)
        return True

    async def dismiss_dlq_message(self, event_id: str) -> bool:
        return await self._update_dlq(event_id, 'dlq-dismissed')

    async def dismiss_all_dlq(self) -> int:
        status = await self._pool.execute(
            "UPDATE events SET action = 'dlq-dismissed' WHERE action = 'dead-letter'",
        )
        return _affected(status)

    async def has_schedules_for_app(self, app_name: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM scheduled_operations WHERE app_name = $1 LIMIT 1", app_name,
        )
        return row is not None

    async def list_remediations_by_agent(self, agent_name: str) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM remediations WHERE agent_name = $1 ORDER BY created_at DESC",
            agent_name,
        )
        return _rows_to_dicts(rows)

    # ── Assessment history / trends ─────────────────────────────────────

    async def list_history(self, repo_url: str) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT id, repo_name, repo_url, assessed_at, overall_score, criticality
            FROM assessments WHERE repo_url = $1 ORDER BY assessed_at ASC
            """,
            repo_url,
        )
        return _rows_to_dicts(rows)

    async def get_trend(self, repo_url: str) -> dict:
        history = await self.list_history(repo_url)
        if not history:
            return {
                "current_score": None,
                "previous_score": None,
                "delta": None,
                "assessments_count": 0,
            }
        current = history[-1]["overall_score"]
        previous = history[-2]["overall_score"] if len(history) >= 2 else None
        delta = round(current - previous, 2) if previous is not None else None
        return {
            "current_score": current,
            "previous_score": previous,
            "delta": delta,
            "assessments_count": len(history),
        }

    # ── Fleet ──────────────────────────────────────────────────────────

    async def repo_urls_with_onboarding(self) -> set[str]:
        """Repo URLs that have at least one onboarding_results row (any assessment).

        Used so Fleet can offer a single "Refresh Onboard" CTA for apps that
        already generated manifests — re-assess alone would drop lifecycle
        back to assessed and force a second Onboard click.
        """
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT a.repo_url
            FROM onboarding_results o
            JOIN assessments a ON a.id = o.assessment_id
            """
        )
        return {r["repo_url"] for r in rows}

    async def repo_has_onboarding(self, repo_url: str) -> bool:
        """True if any historical assessment of this repo was onboarded."""
        row = await self._pool.fetchrow(
            """
            SELECT 1
            FROM onboarding_results o
            JOIN assessments a ON a.id = o.assessment_id
            WHERE a.repo_url = $1
            LIMIT 1
            """,
            repo_url,
        )
        return row is not None

    async def get_fleet_data(self) -> list[dict]:
        """Return one row per unique repo_url with latest assessment + trend."""
        rows = await self._pool.fetch(
            """
            SELECT a.id, a.repo_url, a.repo_name, a.assessed_at,
                   a.overall_score, a.criticality, a.report_json,
                   apps.infra_repo_url AS app_infra_repo_url
            FROM assessments a
            INNER JOIN (
                SELECT repo_url, MAX(assessed_at) AS max_at
                FROM assessments GROUP BY repo_url
            ) latest ON a.repo_url = latest.repo_url
                    AND a.assessed_at = latest.max_at
            LEFT JOIN apps ON apps.repo_url = a.repo_url
            ORDER BY a.overall_score ASC
            """
        )

        ever_onboarded = await self.repo_urls_with_onboarding()
        fleet: list[dict] = []
        for r in rows:
            report = AssessmentReport.model_validate_json(r["report_json"])
            critical_count = sum(
                1 for s in report.scores for f in s.findings
                if f.severity in (Severity.critical, Severity.high)
            )
            trend = await self.get_trend(r["repo_url"])
            fleet.append({
                "id": r["id"],
                "repo_url": r["repo_url"],
                "repo_name": r["repo_name"],
                "latest_score": r["overall_score"],
                "previous_score": trend["previous_score"],
                "delta": trend["delta"],
                "criticality": r["criticality"],
                "last_assessed": r["assessed_at"].isoformat(),
                "assessment_count": trend["assessments_count"],
                "critical_count": critical_count,
                # Read from the `apps` table (the authoritative,
                # always-current source), not this specific assessment's
                # own `report_json`.
                "infra_repo_url": r["app_infra_repo_url"],
                # Prior onboard of any assessment for this repo — drives
                # Fleet's chained "Refresh Onboard" CTA.
                "ever_onboarded": r["repo_url"] in ever_onboarded,
            })
        return fleet

    # ── Gates ────────────────────────────────────────────────────────────

    async def create_gate(self, assessment_id: str, gate_type: str, summary: str) -> str:
        """Create a pending gate, or return the existing one for this app.

        Dedupes by ``repo_url`` + ``gate_type`` (not exact ``assessment_id``):
        gates are app-scoped facts that outlive a single assessment
        (docs/architecture.md). Without the join, re-assess + SLO tracker
        (which iterates every historical assessment) left N pending
        ``rollback-review`` rows of the same type on Actions.
        """
        existing = await self._pool.fetchrow(
            """
            SELECT gates.id FROM gates
            INNER JOIN assessments ON gates.assessment_id = assessments.id
            WHERE assessments.repo_url = (SELECT repo_url FROM assessments WHERE id = $1)
              AND gates.gate_type = $2
              AND gates.status = 'pending'
            ORDER BY gates.created_at DESC
            LIMIT 1
            """,
            assessment_id, gate_type,
        )
        if existing:
            return existing["id"]

        gate_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO gates (id, assessment_id, gate_type, status, summary, created_at)
            VALUES ($1, $2, $3, 'pending', $4, $5)
            """,
            gate_id, assessment_id, gate_type, summary, _now(),
        )
        await self._refresh_active_gates_metric()
        return gate_id

    async def expire_stale_gates(self, hours: int = 24) -> int:
        """Auto-reject pending gates older than the given hours."""
        cutoff = _now() - timedelta(hours=hours)
        status = await self._pool.execute(
            """
            UPDATE gates SET status = 'expired', resolved_at = $1, resolved_by = 'auto-expire'
            WHERE status = 'pending' AND created_at < $2
            """,
            _now(), cutoff,
        )
        count = _affected(status)
        if count:
            await self._refresh_active_gates_metric()
        return count

    async def list_gates(self, status: str = "pending") -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT gates.*, assessments.repo_name AS app_name, assessments.repo_url AS repo_url
            FROM gates LEFT JOIN assessments ON gates.assessment_id = assessments.id
            WHERE gates.status = $1 ORDER BY gates.created_at DESC
            """,
            status,
        )
        return _rows_to_dicts(rows)

    async def list_all_gates(self) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT gates.*, assessments.repo_name AS app_name, assessments.repo_url AS repo_url
            FROM gates LEFT JOIN assessments ON gates.assessment_id = assessments.id
            ORDER BY gates.created_at DESC
            """
        )
        return _rows_to_dicts(rows)

    async def list_gates_for_assessment(self, assessment_id: str, status: str | None = None) -> list[dict]:
        """Keyed off ``repo_url``, joined back through every historical
        assessment of the same app, not an exact ``assessment_id`` match --
        see docs/architecture.md's "Data model: assessments vs. apps"."""
        if status is not None:
            rows = await self._pool.fetch(
                """
                SELECT gates.* FROM gates
                INNER JOIN assessments ON gates.assessment_id = assessments.id
                WHERE assessments.repo_url = (SELECT repo_url FROM assessments WHERE id = $1)
                  AND gates.status = $2
                ORDER BY gates.created_at DESC
                """,
                assessment_id, status,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT gates.* FROM gates
                INNER JOIN assessments ON gates.assessment_id = assessments.id
                WHERE assessments.repo_url = (SELECT repo_url FROM assessments WHERE id = $1)
                ORDER BY gates.created_at DESC
                """,
                assessment_id,
            )
        return _rows_to_dicts(rows)

    async def get_stale_gates(self, hours: int = 24) -> list[dict]:
        """Find pending gates older than the given hours."""
        cutoff = _now() - timedelta(hours=hours)
        rows = await self._pool.fetch(
            "SELECT * FROM gates WHERE status = 'pending' AND created_at < $1 ORDER BY created_at ASC",
            cutoff,
        )
        return _rows_to_dicts(rows)

    async def resolve_gate(self, gate_id: str, status: str, resolved_by: str) -> bool:
        result = await self._pool.execute(
            """
            UPDATE gates SET status = $1, resolved_at = $2, resolved_by = $3
            WHERE id = $4 AND status = 'pending'
            """,
            status, _now(), resolved_by, gate_id,
        )
        changed = _affected(result) > 0
        if changed:
            await self._refresh_active_gates_metric()
        return changed

    # ── Deliveries ───────────────────────────────────────────────────────

    async def create_delivery(
        self,
        assessment_id: str,
        app_name: str,
        categories: dict,
        mechanism: str,
        status: str = "pending",
        details: dict | None = None,
    ) -> str:
        delivery_id = uuid.uuid4().hex
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO deliveries
                (id, assessment_id, app_name, categories_json, mechanism, status, verification, details_json, created_at, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'unknown', $7::jsonb, $8, $9)
            """,
            delivery_id, assessment_id, app_name, json.dumps(categories), mechanism, status,
            json.dumps(details or {}), now, now,
        )
        return delivery_id

    async def update_delivery(
        self,
        delivery_id: str,
        *,
        status: str | None = None,
        verification: str | None = None,
        details: dict | None = None,
    ) -> bool:
        row = await self._pool.fetchrow(
            "SELECT details_json FROM deliveries WHERE id = $1", delivery_id,
        )
        if row is None:
            return False
        merged_details = json.loads(row["details_json"])
        if details:
            merged_details.update(details)
        result = await self._pool.execute(
            """
            UPDATE deliveries SET
                status = COALESCE($2, status),
                verification = COALESCE($3, verification),
                details_json = $4::jsonb,
                updated_at = $5
            WHERE id = $1
            """,
            delivery_id, status, verification, json.dumps(merged_details), _now(),
        )
        return _affected(result) > 0

    async def get_delivery(self, delivery_id: str) -> dict | None:
        row = await self._pool.fetchrow("SELECT * FROM deliveries WHERE id = $1", delivery_id)
        return _delivery_row_to_dict(row)

    async def list_deliveries(self, assessment_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM deliveries WHERE assessment_id = $1 ORDER BY created_at DESC", assessment_id,
        )
        return [_delivery_row_to_dict(r) for r in rows]

    async def list_all_deliveries(self, limit: int = 200) -> list[dict]:
        """Fleet-wide deliveries, newest first -- read-only accessor for the
        Ledger's global view (docs/ledger-design-spec.md card type F).
        ``list_deliveries()`` above stays scoped to one assessment; nothing
        about that call site changes."""
        rows = await self._pool.fetch(
            "SELECT * FROM deliveries ORDER BY created_at DESC LIMIT $1", limit,
        )
        return [_delivery_row_to_dict(r) for r in rows]

    async def list_pending_gitops_deliveries(self) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM deliveries WHERE mechanism = 'infra-repo-commit' AND verification = 'unknown' "
            "ORDER BY created_at ASC",
        )
        return [_delivery_row_to_dict(r) for r in rows]

    # ── Remediations ───────────────────────────────────────────────────

    async def save_remediation(
        self,
        assessment_id: str,
        agent_name: str,
        description: str,
        status: str = "generated",
        manifest_path: str | None = None,
    ) -> str:
        existing = await self._pool.fetchrow(
            """
            SELECT id, status FROM remediations
            WHERE assessment_id = $1 AND agent_name = $2 AND description = $3
              AND status NOT IN ('completed', 'applied')
            LIMIT 1
            """,
            assessment_id, agent_name, description,
        )
        if existing:
            if status != "generated" and status != existing["status"]:
                await self._pool.execute(
                    "UPDATE remediations SET status = $1 WHERE id = $2",
                    status, existing["id"],
                )
            return existing["id"]
        rem_id = uuid.uuid4().hex
        await self._pool.execute(
            """
            INSERT INTO remediations (id, assessment_id, agent_name, description, status, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            rem_id, assessment_id, agent_name, description, status, _now(),
        )
        return rem_id

    async def update_remediation_status(self, remediation_id: str, status: str) -> bool:
        result = await self._pool.execute(
            "UPDATE remediations SET status = $1 WHERE id = $2 AND status != 'completed'",
            status, remediation_id,
        )
        if status == "completed":
            await self._pool.execute(
                "UPDATE remediations SET completed_at = $1 WHERE id = $2 AND status = 'completed'",
                _now(), remediation_id,
            )
        return _affected(result) > 0

    async def list_remediations(self, assessment_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM remediations WHERE assessment_id = $1 ORDER BY created_at DESC",
            assessment_id,
        )
        return _rows_to_dicts(rows)

    async def delete_remediation(self, remediation_id: str, assessment_id: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM remediations WHERE id = $1 AND assessment_id = $2",
            remediation_id, assessment_id,
        )
        return _affected(result) > 0

    async def complete_remediation(self, remediation_id: str) -> bool:
        result = await self._pool.execute(
            """
            UPDATE remediations SET status = 'completed', completed_at = $1
            WHERE id = $2 AND status != 'completed'
            """,
            _now(), remediation_id,
        )
        return _affected(result) > 0

    # ── Agent Registry ─────────────────────────────────────────────────

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

    # ── SLOs ───────────────────────────────────────────────────────────

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
        ``list_gates_for_assessment()`` already has.

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

    # ── Assessment Jobs ──────────────────────────────────────────────────

    async def create_assessment_job(
        self, repo_url: str, continue_onboard: bool = False,
    ) -> str:
        """Create a tracking job for an async assessment run.

        When ``continue_onboard`` is True, ``steps_completed`` starts with
        ``["continue_onboard"]`` so ``assess_progress`` can chain into
        onboarding after the scorecard is saved (Fleet "Refresh Onboard").
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

    # ── Remediation Jobs ──────────────────────────────────────────────────

    async def create_remediation_job(self, assessment_id: str) -> str:
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
        now = _now()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if current_step:
                    row = await conn.fetchrow(
                        "SELECT steps_completed FROM remediation_jobs WHERE id = $1", job_id,
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

    # ── Scheduled Operations ─────────────────────────────────────────

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

    async def update_schedule_cron(self, schedule_id: str, schedule: str) -> bool:
        result = await self._pool.execute(
            "UPDATE scheduled_operations SET schedule = $1, updated_at = $2 WHERE id = $3",
            schedule, _now(), schedule_id,
        )
        return _affected(result) > 0

    async def delete_schedule(self, schedule_id: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM scheduled_operations WHERE id = $1", schedule_id,
        )
        return _affected(result) > 0

    async def toggle_schedule(self, schedule_id: str, enabled: bool) -> bool:
        result = await self._pool.execute(
            "UPDATE scheduled_operations SET enabled = $1, updated_at = $2 WHERE id = $3",
            enabled, _now(), schedule_id,
        )
        return _affected(result) > 0

    # ── Webhook Deduplication ────────────────────────────────────────────

    async def webhook_already_processed(self, delivery_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM processed_webhooks WHERE delivery_id = $1", delivery_id,
        )
        return row is not None

    async def mark_webhook_processed(self, delivery_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO processed_webhooks (delivery_id, processed_at) VALUES ($1, $2) "
            "ON CONFLICT (delivery_id) DO NOTHING",
            delivery_id, _now(),
        )

    async def claim_webhook(self, delivery_id: str) -> bool:
        """Atomically claim a webhook delivery for processing.

        Unlike `webhook_already_processed()` + `mark_webhook_processed()`
        (a check-then-act pair with a race window between the two round
        trips), this does the check-and-mark as a single INSERT relying on
        `processed_webhooks.delivery_id`'s PRIMARY KEY constraint: only one
        concurrent caller for a given delivery_id can ever get a row back
        from `RETURNING`. Callers must call this *before* doing any of the
        delivery's real work, and only proceed if it returns True.
        """
        row = await self._pool.fetchrow(
            "INSERT INTO processed_webhooks (delivery_id, processed_at) VALUES ($1, $2) "
            "ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id",
            delivery_id, _now(),
        )
        return row is not None

    # ── Agent Feedback ──────────────────────────────────────────────────

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

    async def get_feedback_for_app(
        self,
        app_name: str,
        agent_name: str = "",
        finding_category: str = "",
    ) -> list[dict]:
        """Get feedback history for an app, optionally filtered by agent/category."""
        query = "SELECT * FROM agent_feedback WHERE app_name = $1"
        params: list[str] = [app_name]
        if agent_name:
            params.append(agent_name)
            query += f" AND agent_name = ${len(params)}"
        if finding_category:
            params.append(finding_category)
            query += f" AND finding_category = ${len(params)}"
        query += " ORDER BY created_at DESC"
        rows = await self._pool.fetch(query, *params)
        return _rows_to_dicts(rows)

    async def get_all_feedback(self, limit: int = 50) -> list[dict]:
        """Fleet-wide feedback history across all apps, most recent first.

        Used by the Insights page — ``get_feedback_for_app("")`` filters on
        ``WHERE app_name = ''`` and always returns nothing useful, so this is
        the fleet-wide equivalent for that view.
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

    async def get_human_override(self, app_name: str, finding_category: str) -> str | None:
        """Get the most recent human override value for this app/category."""
        row = await self._pool.fetchrow(
            """SELECT human_value FROM agent_feedback
               WHERE app_name = $1 AND finding_category = $2 AND action = 'modified' AND human_value != ''
               ORDER BY created_at DESC LIMIT 1""",
            app_name, finding_category,
        )
        return row["human_value"] if row else None

    # ── Trust / Transparency ────────────────────────────────────────────

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

    # ── Agent Runs ───────────────────────────────────────────────────────

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

    async def list_agent_runs_for_assessment(self, assessment_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM agent_runs WHERE assessment_id = $1 ORDER BY started_at ASC",
            assessment_id,
        )
        return _rows_to_dicts(rows)

    # ── Check Result Snapshots ───────────────────────────────────────────

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

    async def get_assessment_timeline(self, assessment_id: str) -> list[dict]:
        """Get chronological timeline of all events for an assessment."""
        events = await self._pool.fetch(
            """SELECT timestamp, agent_id, action, target_app, severity, summary
               FROM events
               WHERE details_json::text LIKE $1 OR summary LIKE $2
               ORDER BY timestamp ASC""",
            f'%{assessment_id}%', f'%{assessment_id[:12]}%',
        )

        gates = await self._pool.fetch(
            "SELECT created_at as timestamp, 'gate' as agent_id, gate_type as action, status as severity, summary FROM gates WHERE assessment_id = $1 ORDER BY created_at ASC",
            assessment_id,
        )

        remeds = await self._pool.fetch(
            "SELECT created_at as timestamp, agent_name as agent_id, 'remediation' as action, status as severity, description as summary FROM remediations WHERE assessment_id = $1 ORDER BY created_at ASC",
            assessment_id,
        )

        timeline = _rows_to_dicts(events) + _rows_to_dicts(gates) + _rows_to_dicts(remeds)
        timeline.sort(key=lambda x: x.get("timestamp", ""))
        return timeline

    async def get_fleet_insights(self) -> dict:
        """Get fleet-wide statistics for the insights dashboard."""
        total_assessments = await self._pool.fetchval("SELECT COUNT(*) FROM assessments") or 0
        unique_apps = await self._pool.fetchval("SELECT COUNT(DISTINCT repo_url) FROM assessments") or 0
        total_onboardings = await self._pool.fetchval("SELECT COUNT(*) FROM onboarding_results") or 0
        total_remediations = await self._pool.fetchval("SELECT COUNT(*) FROM remediations") or 0
        pending_gates = await self._pool.fetchval("SELECT COUNT(*) FROM gates WHERE status = 'pending'") or 0
        total_events = await self._pool.fetchval("SELECT COUNT(*) FROM events") or 0

        row = await self._pool.fetchrow(
            "SELECT COUNT(*) as total, SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) as rejections FROM agent_feedback"
        )
        total_feedback = row["total"] if row else 0
        total_rejections = (row["rejections"] or 0) if row else 0

        return {
            "total_assessments": total_assessments,
            "unique_apps": unique_apps,
            "total_onboardings": total_onboardings,
            "total_remediations": total_remediations,
            "pending_gates": pending_gates,
            "total_events": total_events,
            "total_feedback": total_feedback,
            "total_rejections": total_rejections,
        }

    async def get_score_history(self, repo_url: str, limit: int = 20) -> list[dict]:
        """Get score history for trend visualization."""
        rows = await self._pool.fetch(
            """SELECT id, assessed_at, overall_score, criticality
               FROM assessments WHERE repo_url = $1
               ORDER BY assessed_at DESC LIMIT $2""",
            repo_url, limit,
        )
        return _rows_to_dicts(list(reversed(rows)))

    # ── Skill Effectiveness ──────────────────────────────────────────

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

    # ── Check Suppression ───────────────────────────────────────────

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

    async def get_suppressed_sources(self, app_name: str) -> set[str]:
        rows = await self._pool.fetch(
            "SELECT check_source FROM suppressed_checks WHERE app_name = $1", app_name,
        )
        return {row["check_source"] for row in rows}

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

                status = await conn.execute(
                    "DELETE FROM remediations WHERE status = 'completed' AND completed_at < $1",
                    cutoff,
                )
                counts["remediations"] = _affected(status)

                status = await conn.execute(
                    "DELETE FROM gates WHERE status IN ('approved', 'rejected', 'expired', 'cancelled') "
                    "AND resolved_at < $1",
                    cutoff,
                )
                counts["gates"] = _affected(status)

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

    # ── DB size / row-count metrics ──────────────────────────────────────

    _METRIC_TABLES = (
        "assessments", "apps", "onboarding_results", "events", "gates", "remediations",
        "agent_registry", "slos", "apply_results", "remediation_jobs",
        "scheduled_operations", "agent_feedback", "skill_effectiveness",
        "agent_runs", "check_results", "deliveries",
    )

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


async def create_store(
    dsn: str | None = None,
    *,
    min_size: int = 5,
    max_size: int = 20,
) -> AssessmentStore:
    """Thin, backend-agnostic-in-name-only convenience wrapper around
    ``AssessmentStore.create()``.

    Every caller in this codebase used to go through
    ``store_factory.create_store()`` while a SQLite/Postgres backend
    selection existed (``AGENTIT_DB_BACKEND``). That selection is gone --
    Postgres is the only store -- but this function is kept as the one,
    consistent construction seam every caller (CLI commands, watchers,
    the portal) already uses, so those call sites don't all need an
    additional, purely-cosmetic rename on top of everything else this
    cutover already touches.
    """
    return await AssessmentStore.create(dsn, min_size=min_size, max_size=max_size)
