"""Async Postgres-backed replacement for ``store.py``'s ``AssessmentStore``.

**Not wired into the application yet.** See ``docs/postgres-migration-plan.md``
for the full design (§2-§4 especially — this file implements Phase 1's schema
and Phase 2's rewrite from that plan). Nothing in ``src/agentit`` imports this
module today; every one of the ~15 call sites listed in the plan's §1 still
talks to the synchronous SQLite ``AssessmentStore`` in ``store.py``.

This class intentionally mirrors ``store.py`` method-for-method (same names,
same parameter shapes, same return shapes) so that Phase 3 of the plan — the
mechanical `def -> async def` / add-`await` pass across those ~15 files and
their tests — is a name/import swap plus `await`, not a logic rewrite.

Two deliberate shape-preservation choices, so callers see identical dict
shapes to the SQLite version despite the underlying type changes described in
the plan's §4:

- Timestamp columns are ``TIMESTAMPTZ`` (per the plan), but ``asyncpg``
  returns those as ``datetime`` objects, not the ISO-8601 strings SQLite's
  ``TEXT`` columns returned. ``_row_to_dict`` below converts any ``datetime``
  value back to its ``.isoformat()`` string on the way out, so every
  dict-returning method here yields the exact same shape as its ``store.py``
  counterpart.
- ``dry_run``/``enabled`` are real ``BOOLEAN`` columns here (SQLite had no
  boolean type, so those were ``INTEGER`` 0/1). Dict output for those two
  fields is therefore ``bool`` here vs. ``int`` in ``store.py`` — a
  deliberate, documented improvement (see plan §4), not an oversight. Every
  other field matches exactly.
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
    """Exponential recency weight for a ``skill_effectiveness`` row -- see
    ``store.py``'s identical helper for the rationale. Accepts either a
    native ``datetime`` (what asyncpg returns for a ``TIMESTAMPTZ`` column)
    or an ISO-8601 string, so this stays usable regardless of whether a row
    came straight from ``asyncpg`` or from a dict already normalized by
    ``_row_to_dict``. Malformed/missing timestamps fall back to full weight
    (1.0) rather than dropping the row.
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


# Phase 1 of docs/postgres-migration-plan.md: idempotent DDL for all 16
# tables, translated from store.py's inline CREATE TABLE statements per the
# type-mapping table in that plan's §4. Run once (see `AssessmentStore.create`)
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
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);

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
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
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
-- heuristic previously used by get_agent_stats(). Mirrors store.py's
-- agent_runs table (see plan §4's type-mapping table: TIMESTAMPTZ for
-- started_at, plain INTEGER for duration_ms since it's a measurement,
-- not a boolean flag).
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
-- compliance reporting. `passed` is BOOLEAN here (store.py's INTEGER-as-
-- boolean per plan §4's type-mapping table), not INTEGER.
CREATE TABLE IF NOT EXISTS check_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    dimension TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_results_assessment ON check_results(assessment_id);

-- Migration: add correlation_id to events created before this column
-- existed. Postgres supports ADD COLUMN IF NOT EXISTS natively, so (per
-- plan §4) this needs none of store.py's try/except OperationalError dance.
ALTER TABLE events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
CREATE INDEX IF NOT EXISTS idx_events_correlation_id ON events(correlation_id);
"""

_ALL_TABLES = [
    "assessments", "onboarding_results", "events", "gates",
    "remediations", "agent_registry", "slos", "apply_results",
    "settings", "remediation_jobs", "scheduled_operations",
    "processed_webhooks", "agent_feedback", "skill_effectiveness",
    "suppressed_checks", "skill_inventory_snapshots",
    "agent_runs", "check_results",
]


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    """Convert an asyncpg Record to a dict, normalizing datetimes to
    ISO-8601 strings so callers see the same shape SQLite's TEXT columns
    produced (see module docstring)."""
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


def _rows_to_dicts(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


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
    """Async, Postgres-backed counterpart to ``portal.store.AssessmentStore``.

    Construct via the ``create()`` classmethod (pool creation is inherently
    async, so a plain ``__init__`` can't do it — see plan §9 Phase 2).
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

    async def save(self, report: AssessmentReport) -> str:
        assessment_id = uuid.uuid4().hex
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

    async def get_fleet_data(self) -> list[dict]:
        """Return one row per unique repo_url with latest assessment + trend."""
        rows = await self._pool.fetch(
            """
            SELECT a.id, a.repo_url, a.repo_name, a.assessed_at,
                   a.overall_score, a.criticality, a.report_json
            FROM assessments a
            INNER JOIN (
                SELECT repo_url, MAX(assessed_at) AS max_at
                FROM assessments GROUP BY repo_url
            ) latest ON a.repo_url = latest.repo_url
                    AND a.assessed_at = latest.max_at
            ORDER BY a.overall_score ASC
            """
        )

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
            })
        return fleet

    # ── Gates ────────────────────────────────────────────────────────────

    async def create_gate(self, assessment_id: str, gate_type: str, summary: str) -> str:
        existing = await self._pool.fetchrow(
            "SELECT id FROM gates WHERE assessment_id = $1 AND gate_type = $2 AND status = 'pending'",
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
        return _affected(status)

    async def list_gates(self, status: str = "pending") -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT * FROM gates WHERE status = $1 ORDER BY created_at DESC", status,
        )
        return _rows_to_dicts(rows)

    async def list_all_gates(self) -> list[dict]:
        rows = await self._pool.fetch("SELECT * FROM gates ORDER BY created_at DESC")
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
        return _affected(result) > 0

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
            VALUES ($1, $2, $3, 'active', $4::jsonb, $5, $6)
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
            VALUES ($1, $2, $3, 'active', '[]'::jsonb, $4, $4)
            ON CONFLICT (agent_name) DO UPDATE SET last_heartbeat = EXCLUDED.last_heartbeat
            """,
            uuid.uuid4().hex, agent_name, category, now,
        )
        return True

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
        rows = await self._pool.fetch(
            "SELECT * FROM slos WHERE assessment_id = $1 ORDER BY metric_name", assessment_id,
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
        result = await self._pool.execute(
            "DELETE FROM slos WHERE id = $1 AND assessment_id = $2",
            slo_id, assessment_id,
        )
        return _affected(result) > 0

    # ── Assessment Jobs ──────────────────────────────────────────────────

    async def create_assessment_job(self, repo_url: str) -> str:
        """Create a tracking job for an async assessment run."""
        job_id = uuid.uuid4().hex
        now = _now()
        await self._pool.execute(
            """INSERT INTO remediation_jobs
                (id, assessment_id, status, current_step, steps_completed, error, created_at, updated_at)
            VALUES ($1, $2, 'assessing', $3, '[]'::jsonb, '', $4, $5)""",
            job_id, "", repo_url[:200], now, now,
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
        """Get performance stats per agent: total runs, success rate, avg events."""
        query = """
            SELECT agent_id,
                   COUNT(*) as total_events,
                   SUM(CASE WHEN action LIKE '%complete%' THEN 1 ELSE 0 END) as successes,
                   SUM(CASE WHEN action LIKE '%failed%' OR action LIKE '%error%' THEN 1 ELSE 0 END) as failures,
                   MIN(timestamp) as first_seen,
                   MAX(timestamp) as last_seen
            FROM events
        """
        params: list[str] = []
        if agent_name:
            params.append(agent_name)
            query += " WHERE agent_id = $1"
        query += " GROUP BY agent_id ORDER BY total_events DESC"
        rows = await self._pool.fetch(query, *params)
        stats = []
        for r in rows:
            total = r["successes"] + r["failures"]
            success_rate = (r["successes"] / total * 100) if total > 0 else 0
            stats.append({
                "agent": r["agent_id"],
                "total_events": r["total_events"],
                "successes": r["successes"],
                "failures": r["failures"],
                "success_rate": round(success_rate, 1),
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

        See ``store.py``'s identical method for the full rationale --
        mirrored here row-for-row (fetch raw ``created_at`` per row and
        weight in Python) rather than aggregated in SQL, so the weighting
        logic is identical across both backends.
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
        """Skills flagged for review by their recency-weighted approval rate
        -- see ``store.py``'s identical method for the rationale."""
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
        """Self-improvement loop health -- see ``store.py``'s identical
        method for the rationale."""
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
        """Per-skill lifecycle view -- see ``store.py``'s identical method
        for the rationale."""
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
        """Export all tables as JSON for disaster recovery."""
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

                # Postgres (unlike SQLite) rejects selecting a non-grouped,
                # non-aggregated column in a GROUP BY query, so the
                # "keep the latest row per assessment_id" subquery needs
                # DISTINCT ON instead of store.py's GROUP BY/HAVING MAX().
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
        "assessments", "onboarding_results", "events", "gates", "remediations",
        "agent_registry", "slos", "apply_results", "remediation_jobs",
        "scheduled_operations", "agent_feedback", "skill_effectiveness",
        "agent_runs", "check_results",
    )

    async def get_db_stats(self) -> dict:
        """Row counts per table plus the database size, for the
        `agentit_db_size_bytes` / `agentit_db_rows` Prometheus gauges.

        There's no single on-disk "file" to `os.path.getsize()` the way
        store.py does for its SQLite file — `pg_database_size()` is the
        Postgres equivalent of "how big is this database right now".
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
