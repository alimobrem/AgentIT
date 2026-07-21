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

**Package layout (2026-07-20 domain split).** This used to be one 2685-line
``store.py`` file with ~109 methods on a single class -- flagged by an
external reuse/refactor review as the codebase's "god store" and hardest
test seam. It is now a package: this ``__init__.py`` defines the
``AssessmentStore`` facade class itself (plus ``create_store()`` and every
module-level symbol external callers already import, e.g.
``normalize_repo_url``/``SCHEMA_SQL``/``_ALL_TABLES``/``ASSESSMENT_CADENCES``,
all re-exported here from ``_shared.py``), while every one of its ~109
public methods now *lives* in a domain-specific mixin module alongside it
(``assessments.py``, ``events.py``, ``fleet.py``, ``deliveries.py``,
``jobs.py``, ``schedules.py``, ``agents.py``, ``skills.py``,
``feedback.py``, ``checks.py``, ``slos.py``, ``admin.py``) and
``AssessmentStore`` inherits from all of them.

**Why mixins, not composition.** Every one of the original class's ~109
methods depends on exactly one piece of shared state -- ``self._pool``,
the ``asyncpg.Pool`` set once in ``__init__`` below -- and nothing else (no
locks, no other instance attributes; verified by grepping the original file
for every ``self.<x> =`` assignment before starting this split). Several
methods also call sibling methods that now live in a *different* domain
module (``save()`` in ``assessments.py`` calls ``self.log_event(...)``,
which lives in ``events.py``; ``get_fleet_data()`` in ``fleet.py`` calls
``self.get_trend()``, which lives in ``assessments.py``). Multiple
inheritance (mixins) preserves this exactly: every domain mixin's methods
land in the same flat instance namespace, so ``self.log_event(...)`` from
inside ``assessments.py`` resolves through Python's normal attribute lookup
without either module importing the other -- no facade-forwarding
boilerplate, no ``__getattr__`` indirection, and (this is the part that
actually matters for the "zero behavior change" constraint) the resulting
public method names/signatures on ``AssessmentStore`` are *byte-for-byte*
identical to before the split, because they're the exact same function
objects, just defined in a different file. A composed-domain-objects
design (``self.jobs = JobsDomain(pool)``, called as ``store.jobs.
create_remediation_job(...)``) was considered and rejected specifically
because it would have required renaming every one of the ~2700+ existing
call sites across the whole codebase (``store.create_remediation_job(...)``
-> ``store.jobs.create_remediation_job(...)``) -- exactly the "import
churn"/breaking-API-redesign risk this refactor's own brief says to avoid.

**Where each domain's methods live, and why that boundary:** see each
mixin module's own docstring for the specific tables/methods it owns and
the reasoning behind that grouping (most map 1:1 onto the table(s) they
touch; a couple -- ``jobs.py``/``assessments.py`` -- group two of the
original file's section-comment headers together because they share one
underlying table or one tightly-coupled entity).

**``SCHEMA_SQL`` stayed centralized in ``_shared.py``**, not split
alongside each owning domain -- see that module's docstring for why (the
DDL interleaves several tables' triggers/backfill logic in ways that don't
cleanly decompose per-domain, and centralizing it is the lower-risk,
easier-to-audit-in-one-read option for something that runs once at every
``create()``).
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

from ._shared import (
    ASSESSMENT_CADENCE_INTERVALS,
    ASSESSMENT_CADENCES,
    SCHEMA_SQL,
    _ALL_TABLES,
    _affected,
    _delivery_row_to_dict,
    _now,
    _pr_outcome_row_to_dict,
    _recency_weight,
    _row_to_dict,
    _rows_to_dicts,
    normalize_repo_url,
)

logger = logging.getLogger(__name__)
from .assessments import AssessmentsMixin

__all__ = [
    "AssessmentStore",
    "create_store",
    "normalize_repo_url",
    "SCHEMA_SQL",
    "ASSESSMENT_CADENCES",
    "ASSESSMENT_CADENCE_INTERVALS",
]


# Idempotent DDL for all 19 tables. Run once (see `AssessmentStore.create`)
# via a single multi-statement `execute()` call — asyncpg uses the simple
# query protocol (which permits multiple `;`-separated statements) whenever
# `execute()` is called with no bind parameters.
SCHEMA_SQL = r"""
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
-- Additive, idempotent column (same no-migration-framework convention used
-- throughout this file): how often this app should be automatically
-- re-assessed by watchers/reassess_scheduler.py, independent of the
-- push-triggered/manual re-Assess paths that already existed. 'daily' is
-- the default for every app (including ones onboarded before this column
-- existed) so "default to 24 hours" holds without a separate backfill --
-- see get_apps_due_for_reassessment()/set_assessment_cadence() below.
ALTER TABLE apps ADD COLUMN IF NOT EXISTS assessment_cadence TEXT NOT NULL DEFAULT 'daily';

-- Structural (DB-layer) backstop for the whole "duplicate Fleet row" bug
-- class -- see `normalize_repo_url()`'s docstring for the app-level
-- version of this same logic, which this SQL mirrors exactly. Without
-- this, `apps.repo_url`'s PRIMARY KEY only dedupes exact-string matches;
-- two different raw spellings of the same repo (a `.git` suffix, a
-- trailing slash) still land as two rows, since nothing forces a write
-- to go through the Python `normalize_repo_url()` first. This trigger
-- makes that structurally impossible for ANY future INSERT/UPDATE of
-- `repo_url` on either table -- app code, a one-off migration script, a
-- CI pipeline step with a stale hardcoded URL -- regardless of whether it
-- remembers to normalize. (`assessments` intentionally allows many rows
-- per app over time, so it can't carry its own uniqueness constraint the
-- way `apps` can -- this trigger still keeps every one of those rows'
-- `repo_url` canonical, which is what `get_fleet_data()`'s
-- `GROUP BY repo_url` actually depends on.) Pre-existing non-normalized
-- rows this can't retroactively fix are healed by
-- `AssessmentStore.dedupe_repo_urls()`, run once at every `create()` and
-- periodically by the background maintenance loop (see `app.py`).
CREATE OR REPLACE FUNCTION agentit_normalize_repo_url(url TEXT) RETURNS TEXT AS $$
    SELECT regexp_replace(rtrim(url, '/'), '\.git$', '', 'i');
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION agentit_normalize_repo_url_trigger() RETURNS TRIGGER AS $$
BEGIN
    NEW.repo_url := agentit_normalize_repo_url(NEW.repo_url);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS normalize_repo_url_before_write ON assessments;
CREATE TRIGGER normalize_repo_url_before_write
    BEFORE INSERT OR UPDATE OF repo_url ON assessments
    FOR EACH ROW EXECUTE FUNCTION agentit_normalize_repo_url_trigger();

DROP TRIGGER IF EXISTS normalize_repo_url_before_write ON apps;
CREATE TRIGGER normalize_repo_url_before_write
    BEFORE INSERT OR UPDATE OF repo_url ON apps
    FOR EACH ROW EXECUTE FUNCTION agentit_normalize_repo_url_trigger();

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

-- Remediations has been removed as a standalone concept: rows were keyed
-- only by (assessment_id, agent_name, description) with zero link to the
-- delivery/PR that actually resolved them, so "completion" was always
-- a hand-maintained flag (a manual "Mark Done" button as the only fallback)
-- -- see deliveries.details_json.outcomes.*.pr_url / onboarding_results.
-- pr_url (aggregated by pr_tracking.py) for the real, honestly-derived
-- fix/PR status this table never actually tracked. This DROP is a one-time,
-- idempotent cleanup for any database that already created the table
-- before this line landed; confirmed nothing of value is lost -- every
-- generated-fix/delivery outcome that ever mattered is already durably
-- recorded via `events`/`deliveries` independent of this table.
DROP TABLE IF EXISTS remediations;

-- The generic `gates` table (and its `resolve_gate()`/`create_gate()`
-- machinery, routes/gates.py) has been removed entirely, 2026-07-19: every
-- delivery now ends in a real GitHub PR that requires a human merge --
-- that IS the approval step now, for every category (see pr_tracking.py's
-- module docstring and routes/pr_actions.py's Merge/Close actions).
-- rollback-review/finding-unresolved-escalation (which never had a PR to
-- track) became plain, unresolved `events` rows instead (see
-- store.list_unresolved_events()/routes/recommendations.py). Confirmed
-- nothing of value is lost: every real PR/delivery outcome that ever
-- mattered is already durably recorded via `deliveries`/`onboarding_
-- results`/`pr_outcomes`, independent of this table; a rejection reason
-- that used to live only in a gate-resolution's `agent_feedback` write is
-- now captured directly from the real GitHub PR close comment
-- (pr_outcomes.py), not lost.
DROP TABLE IF EXISTS gates;

-- Durable, queryable record of what happened to a real GitHub PR AgentIT
-- opened, once it's known to be closed without merging (a real rejection)
-- or merged with extra commits a human pushed first (a real pre-merge
-- edit) -- see pr_outcomes.py. Never overwritten once recorded (`pr_url`
-- is UNIQUE; callers check-then-insert) so a future "learn from this"
-- mechanism can query "what's the history of rejections/edits for content
-- generated by skill X / for finding-category Y / for app Z" without the
-- raw evidence having been thrown away after being shown once. No FK on
-- `assessment_id` (unlike `gates`/`deliveries`) -- deliberately: this data
-- must outlive `AssessmentStore.delete()`'s per-app cascade, the same way
-- `agent_feedback`/`skill_effectiveness` already do, so deleting an app
-- from the fleet never erases what was learned about its past PRs.
CREATE TABLE IF NOT EXISTS pr_outcomes (
    id TEXT PRIMARY KEY,
    pr_url TEXT NOT NULL UNIQUE,
    assessment_id TEXT,
    app_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    finding_category TEXT NOT NULL DEFAULT '',
    skill_names_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    outcome TEXT NOT NULL,
    reject_reason TEXT NOT NULL DEFAULT '',
    edit_diff_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    detected_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_outcomes_app ON pr_outcomes(app_name);
CREATE INDEX IF NOT EXISTS idx_pr_outcomes_finding_category ON pr_outcomes(finding_category);

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

-- `schedule`/`enabled` are effectively frozen at creation time: their only
-- mutators (`update_schedule_cron()`/`toggle_schedule()`, and the
-- `POST /schedules/update`/`POST /schedules/toggle` routes that called
-- them) were dead code -- self-documented as unreachable -- and were
-- deleted 2026-07-20. No live bug; this is just a schema note for why a
-- row's `schedule`/`enabled` never change after `create_schedule()`.
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

-- Per-app mutex for the actual delivery-commit step (route_and_deliver()),
-- closing the race github_pr.py's fixed agentit/{app} branch name +
-- force-push-on-conflict fallback otherwise leaves open between two
-- overlapping deliveries for the same app (see claim_delivery_lock()).
CREATE TABLE IF NOT EXISTS delivery_locks (
    lock_key TEXT PRIMARY KEY,
    claimed_at TIMESTAMPTZ NOT NULL
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
-- Finding-scoped re-verification (docs/onboarding-loop-vision-gap-analysis.md
-- Phase 3): which specific finding(s) -- keyed the same
-- (category, description.lower()[:80]) shape assessment_diff.py's
-- diff_assessments() dedups findings on -- this delivery was meant to
-- resolve, and whether a later push-triggered re-assessment confirmed it
-- actually did. ``finding_resolution`` is deliberately a separate column
-- from ``verification`` above: that one tracks post-delivery SLO health
-- (verify_and_close_delivery()); this one tracks whether the specific
-- finding the delivery targeted stopped showing up on re-assessment -- two
-- different questions about the same delivery, so conflating them into one
-- column would lose one or the other. Additive columns on an already-
-- created table, same no-migration-framework convention used throughout
-- this file.
ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS target_findings_json JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS finding_resolution TEXT;

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
    "assessments", "apps", "onboarding_results", "events",
    "agent_registry", "slos", "apply_results",
    "settings", "remediation_jobs", "scheduled_operations",
    "processed_webhooks", "agent_feedback", "skill_effectiveness",
    "suppressed_checks", "skill_inventory_snapshots",
    "agent_runs", "check_results", "deliveries", "pr_outcomes",
    "delivery_locks",
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
    d["target_findings"] = [tuple(f) for f in json.loads(d.pop("target_findings_json"))]
    return d


def _pr_outcome_row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    d = _row_to_dict(row)
    if d is None:
        return None
    d["skill_names"] = json.loads(d.pop("skill_names_json"))
    d["edit_diff"] = json.loads(d.pop("edit_diff_json"))
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


def normalize_repo_url(repo_url: str) -> str:
    """Canonicalize a repo URL before it's stored/compared as the identity
    key `get_fleet_data()` (and every other repo-scoped query) groups by.

    Without this, the exact same repo submitted once as e.g.
    ``https://github.com/org/app`` (the form's own placeholder text, and
    what `self_assess_route`'s hardcoded URL uses) and once as
    ``https://github.com/org/app.git`` (the default form GitHub's own
    "Clone" HTTPS URL uses, or a trailing slash from a pasted browser URL)
    are two different strings -- `repo_name`, derived from this same
    string via ``repo_url.rstrip("/").split("/")[-1].removesuffix(".git")``
    (see `runner.py`), happens to normalize those two particular
    differences away for *display*, so Fleet shows two rows with an
    identical name for what a human reasonably expects to be one app.

    Deliberately does not touch letter case: GitHub owner/repo path
    segments are case-preserving in the UI and in every generated display
    name/URL derived from this string, even though GitHub's own routing is
    case-insensitive -- lowercasing here would silently rename every
    already-displayed app and could break exact-string comparisons
    elsewhere (e.g. GitHub API calls) that expect the real casing.
    """
    url = repo_url.strip()
    while url.endswith("/"):
        url = url[:-1]
    if url.lower().endswith(".git"):
        url = url[: -len(".git")]
    return url


# How often watchers/reassess_scheduler.py automatically re-Assesses an app,
# keyed by `apps.assessment_cadence`. 'manual' opts an app out of automatic
# re-assessment entirely (push-triggered/manual re-Assess still work exactly
# as before) -- it has no entry here on purpose, so it can never be treated
# as "due".
ASSESSMENT_CADENCE_INTERVALS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}
ASSESSMENT_CADENCES = (*ASSESSMENT_CADENCE_INTERVALS, "manual")


class AssessmentStore(AssessmentsMixin):
    """The one and only ``AssessmentStore``. Postgres-backed, fully async.

    Construct via the ``create()`` classmethod (pool creation is inherently
    async, so a plain ``__init__`` can't do it).

    Inherits its ~109 public domain methods from the mixin classes imported
    above (one per ``store/<domain>.py`` module) -- see this module's own
    docstring for the full rationale. This class itself only owns the
    lifecycle methods (``__init__``/``create``/``close``) and the shared
    ``self._pool`` every mixin method reads.
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
        command_timeout: float = 30.0,
        connect_timeout: float = 15.0,
    ) -> "AssessmentStore":
        """``command_timeout``/``connect_timeout`` are exposed as
        parameters (not just hardcoded) so a fault-injection test can
        construct a store with an aggressively short bound and prove the
        timeout actually fires against a real, deliberately-wedged query
        (``pg_sleep()``) in well under a second, instead of the real
        30s/15s defaults every production caller gets.
        """
        if dsn is None:
            dsn = os.environ.get("AGENTIT_DB_DSN")
        if not dsn:
            raise ValueError(
                "No Postgres DSN provided and AGENTIT_DB_DSN is not set."
            )
        # `command_timeout`/`timeout` are unset (unbounded) by default in
        # asyncpg -- every query issued through this pool (`fetch`/
        # `fetchrow`/`fetchval`/`execute`) would otherwise wait *forever*
        # against a wedged (not fully down, just stuck -- a lock wait, a
        # runaway query on someone else's connection, a half-open TCP
        # session) Postgres, with no timeout error ever raised. Worse,
        # since every FastAPI route holds its connection for that whole
        # wait, enough concurrently-stuck requests exhaust the pool
        # (`max_size=20`) and every *other* route needing the store hangs
        # too -- a single wedged query cascading into total portal
        # unavailability with zero user-facing signal. `command_timeout`
        # (default 30s) bounds every query to a generous ceiling well above
        # this app's real query shapes (raw hand-written SQL against a
        # modest-sized table set, no multi-second aggregations) while still
        # turning "wedged" into a clear, fast `asyncpg.QueryCanceledError`
        # instead of an indefinite hang. `connect_timeout` (default 15s,
        # vs. asyncpg's own 60s default) bounds each new connection attempt
        # to roughly the same ceiling this app already uses for its other
        # external dependencies (`kube.py`'s `_request_timeout`,
        # `github_pr.py`'s `requests.*` calls) rather than a 60s wait per
        # connection before a caller even learns Postgres is unreachable.
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size,
            command_timeout=command_timeout, timeout=connect_timeout,
        )
        await pool.execute(SCHEMA_SQL)
        store = cls(pool)
        # Heal any repo_url duplicates inherited from before the
        # normalize_repo_url_before_write trigger existed (or from any
        # other gap) right away, not just on the next 5-min maintenance
        # tick -- see dedupe_repo_urls()'s docstring.
        try:
            await store.dedupe_repo_urls()
        except Exception:
            logger.warning("Startup repo_url dedupe failed (non-fatal)", exc_info=True)
        return store

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

    async def list_unresolved_events(
        self, action: str, resolved_actions: list[str], target_app: str | None = None,
    ) -> list[dict]:
        """Every ``action``-typed event with no later event correlated to it
        (``correlation_id`` = the original event's own ``id``) whose own
        ``action`` is one of ``resolved_actions`` -- the lightweight, plain-
        events "still needs a human decision" mechanism that replaced the
        ``gates`` table for recommendations that aren't PR-trackable
        (``rollback-review``, ``finding-unresolved-escalation`` -- see
        ``routes/recommendations.py``). Mirrors the same correlation-id
        chain convention ``list_events_by_correlation_id()`` already uses,
        just inverted: "does this event have a resolving reply" rather than
        "give me every event in one chain". Pass ``target_app`` to scope to
        one app (mirrors ``list_gates_for_assessment()``'s old per-app
        scoping); omit for the fleet-wide view (mirrors ``list_all_gates()``).
        """
        query = """
            SELECT e1.* FROM events e1
            WHERE e1.action = $1
              AND NOT EXISTS (
                SELECT 1 FROM events e2
                WHERE e2.correlation_id = e1.id AND e2.action = ANY($2::text[])
              )
        """
        params: list[Any] = [action, list(resolved_actions)]
        if target_app is not None:
            params.append(target_app)
            query += f" AND e1.target_app = ${len(params)}"
        query += " ORDER BY e1.timestamp DESC"
        rows = await self._pool.fetch(query, *params)
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

    # ── Fleet ──────────────────────────────────────────────────────────

    async def repo_urls_with_onboarding(self) -> set[str]:
        """Repo URLs that have at least one onboarding_results row (any assessment).

        Used so Fleet can offer a single "Scan" CTA for apps that already
        generated manifests — re-assess alone would drop lifecycle back to
        assessed and force a second Onboard click.
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
                # Fleet's chained "Scan" CTA (confirm-gated once true).
                "ever_onboarded": r["repo_url"] in ever_onboarded,
            })
        return fleet

    # ── Deliveries ───────────────────────────────────────────────────────

    async def create_delivery(
        self,
        assessment_id: str,
        app_name: str,
        categories: dict,
        mechanism: str,
        status: str = "pending",
        details: dict | None = None,
        target_findings: list[tuple[str, str]] | None = None,
    ) -> str:
        """``target_findings``, when given, is the ``(category,
        description.lower()[:80])`` key -- the exact shape
        ``assessment_diff.diff_assessments()`` dedups findings on (see
        ``assessment_diff.finding_key()``) -- for the specific finding(s)
        this delivery was generated to resolve. Defaults to empty (unknown/
        not tracked): most historical callers, and any delivery whose files
        don't trace back to one or a few specific findings (e.g. a delivery
        with no report at all), never set this.
        """
        delivery_id = uuid.uuid4().hex
        now = _now()
        await self._pool.execute(
            """
            INSERT INTO deliveries
                (id, assessment_id, app_name, categories_json, mechanism, status, verification, details_json, target_findings_json, created_at, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'unknown', $7::jsonb, $8::jsonb, $9, $9)
            """,
            delivery_id, assessment_id, app_name, json.dumps(categories), mechanism, status,
            json.dumps(details or {}), json.dumps(list(target_findings or [])), now,
        )
        return delivery_id

    async def update_delivery(
        self,
        delivery_id: str,
        *,
        status: str | None = None,
        verification: str | None = None,
        details: dict | None = None,
        finding_resolution: str | None = None,
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
                finding_resolution = COALESCE($5, finding_resolution),
                updated_at = $6
            WHERE id = $1
            """,
            delivery_id, status, verification, json.dumps(merged_details), finding_resolution, _now(),
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

    async def list_deliveries_for_app(self, app_name: str) -> list[dict]:
        """Every delivery for this app, across every one of its historical
        assessments -- ``deliveries.app_name`` is a plain column (not just
        reachable via an ``assessment_id`` join), so this is a direct
        lookup, the same shape ``list_deliveries_pending_finding_check()``
        below already uses for its own ``WHERE app_name = $1``. Used by
        ``pr_tracking.py`` to build one app's full PR History from every
        ``source-repo-pr``/``app-repo-pr`` delivery outcome, not just the
        current assessment's own deliveries (``list_deliveries()`` above).
        """
        rows = await self._pool.fetch(
            "SELECT * FROM deliveries WHERE app_name = $1 ORDER BY created_at DESC", app_name,
        )
        return [_delivery_row_to_dict(r) for r in rows]

    async def list_deliveries_pending_finding_check(self, app_name: str) -> list[dict]:
        """Every delivery for this app that recorded ``target_findings`` (see
        ``create_delivery()``) and hasn't been finding-checked yet
        (``finding_resolution IS NULL``) -- the queue
        ``delivery.check_pending_delivery_verifications()`` walks on every
        push-triggered re-assessment (docs/onboarding-loop-vision-gap-
        analysis.md Phase 3). A delivery with no recorded target findings at
        all (the default for most historical/whole-batch deliveries) never
        shows up here -- there's nothing to correlate.
        """
        rows = await self._pool.fetch(
            """
            SELECT * FROM deliveries
            WHERE app_name = $1 AND finding_resolution IS NULL AND target_findings_json != '[]'::jsonb
            ORDER BY created_at ASC
            """,
            app_name,
        )
        return [_delivery_row_to_dict(r) for r in rows]

    async def get_finding_failure_count(self, app_name: str, finding_category: str) -> int:
        """How many delivery attempts for this app, targeting this finding
        category, have been confirmed (via ``list_deliveries_pending_
        finding_check``'s correlation) to have left their target finding
        still present after the fix? Mirrors ``get_rejection_count()``'s
        exact (app_name, finding_category) counting shape above for the
        same "how many times has X failed" concept -- applied here to a
        machine-confirmed still-broken automated delivery rather than a
        human's explicit gate rejection, so it's counted against
        ``deliveries``, not ``agent_feedback`` (a table documented, and
        consumed elsewhere, as specifically HUMAN feedback).
        """
        row = await self._pool.fetchrow(
            """
            SELECT COUNT(*) as cnt FROM deliveries
            WHERE app_name = $1 AND finding_resolution = 'still_present'
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(target_findings_json) elem
                WHERE elem->>0 = $2
              )
            """,
            app_name, finding_category,
        )
        return row["cnt"] if row else 0

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

    # ── Assessment Jobs ──────────────────────────────────────────────────

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

    # ── Remediation Jobs ──────────────────────────────────────────────────

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
            WHERE status NOT IN ('completed', 'failed') AND created_at < $2
            RETURNING id, assessment_id, current_step
            """,
            _now(), cutoff,
        )
        return _rows_to_dicts(rows)

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

    async def delete_schedule(self, schedule_id: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM scheduled_operations WHERE id = $1", schedule_id,
        )
        return _affected(result) > 0

    # ── Webhook Deduplication ────────────────────────────────────────────

    async def claim_webhook(self, delivery_id: str) -> bool:
        """Atomically claim a webhook delivery for processing.

        Unlike the now-deleted `webhook_already_processed()` +
        `mark_webhook_processed()` (a check-then-act pair with a race
        window between the two round trips), this does the check-and-mark
        as a single INSERT relying on
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

    # ── Delivery Locking ─────────────────────────────────────────────────

    async def claim_delivery_lock(self, lock_key: str, stale_after_seconds: int = 900) -> bool:
        """Atomically claim a per-app mutex around the actual
        delivery-commit step (``portal/delivery.py::route_and_deliver()``).

        ``github_pr.py``'s ``commit_to_infra_repo()`` always targets the
        same fixed branch name (``agentit/{app}``) and force-pushes over
        any existing ref on a 422 after independently re-reading
        ``base_sha`` -- with no optimistic-concurrency check between
        reading it and pushing. Two overlapping deliveries for the same
        app (e.g. the automatic background validate-and-deliver pipeline
        still running while a human clicks "Run Automatic Validation," or
        a Phase-4 ``redispatch_finding_fix()`` racing a fresh manual
        Deliver) could otherwise silently clobber one another via that
        force-push fallback while each independently reports success.

        Uses the same single-round-trip ``INSERT ... RETURNING`` idiom
        ``claim_webhook()`` already established for the identical
        "check-and-mark atomically, no race window between the two"
        problem -- extended here with a staleness override (``ON CONFLICT
        ... DO UPDATE ... WHERE ...``, still one atomic statement) so a
        lock left behind by a process that crashed mid-delivery doesn't
        block that app's deliveries forever: 900s mirrors
        ``reap_orphaned_jobs()``'s existing staleness-recovery window
        (comfortably above ``with_timeout``'s 300s operation ceiling), the
        established precedent in this codebase for "how long can a
        real in-progress operation take before assuming the process that
        started it is gone."

        Callers must call ``release_delivery_lock()`` (in a ``finally``)
        once the delivery-commit step is done, win or lose -- see
        ``route_and_deliver()``.
        """
        row = await self._pool.fetchrow(
            """
            INSERT INTO delivery_locks (lock_key, claimed_at) VALUES ($1, $2)
            ON CONFLICT (lock_key) DO UPDATE SET claimed_at = EXCLUDED.claimed_at
            WHERE delivery_locks.claimed_at < $3
            RETURNING lock_key
            """,
            lock_key, _now(), _now() - timedelta(seconds=stale_after_seconds),
        )
        return row is not None

    async def release_delivery_lock(self, lock_key: str) -> None:
        await self._pool.execute("DELETE FROM delivery_locks WHERE lock_key = $1", lock_key)

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

    # ── PR outcomes (rejections/pre-merge edits, real GitHub-derived) ────
    # See pr_outcomes.py -- durable evidence for a future "learn from this"
    # mechanism, keyed by pr_url (never rewritten once recorded).

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

    async def get_fleet_insights(self) -> dict:
        """Get fleet-wide statistics for the insights dashboard."""
        total_assessments = await self._pool.fetchval("SELECT COUNT(*) FROM assessments") or 0
        unique_apps = await self._pool.fetchval("SELECT COUNT(DISTINCT repo_url) FROM assessments") or 0
        total_onboardings = await self._pool.fetchval("SELECT COUNT(*) FROM onboarding_results") or 0
        # Real PR activity, not a hand-maintained "remediations" completion
        # flag with no link to any actual PR/delivery (see the removed
        # `remediations` table's schema comment) -- a pure DB count across
        # the two places pr_tracking.py documents a `pr_url` can land, with
        # no live GitHub call (mirrors every other stat here).
        # `delivery_pr_count`: a delivery outcome's own pr_url (every
        # category now, including the former gate-tracked cluster_config/
        # cicd_shared_namespace -- the `gates` table has been removed
        # entirely, 2026-07-19). `onboarding_pr_count`: onboarding_results.
        # pr_url, which may itself be several `|`-joined URLs (Per-Agent
        # PRs) -- split and counted individually.
        delivery_pr_count = await self._pool.fetchval(
            """
            SELECT COUNT(*) FROM deliveries d,
                jsonb_each(COALESCE(d.details_json->'outcomes', '{}'::jsonb)) AS outcome(category, value)
            WHERE value->>'pr_url' IS NOT NULL
            """
        ) or 0
        onboarding_pr_rows = await self._pool.fetch(
            "SELECT pr_url FROM onboarding_results WHERE pr_url IS NOT NULL AND pr_url != ''"
        )
        onboarding_pr_count = sum(
            len([u for u in (row["pr_url"] or "").split("|") if u.strip()])
            for row in onboarding_pr_rows
        )
        total_prs = delivery_pr_count + onboarding_pr_count
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
            "total_prs": total_prs,
            "total_events": total_events,
            "total_feedback": total_feedback,
            "total_rejections": total_rejections,
        }

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
        "assessments", "apps", "onboarding_results", "events",
        "agent_registry", "slos", "apply_results", "remediation_jobs",
        "scheduled_operations", "agent_feedback", "skill_effectiveness",
        "agent_runs", "check_results", "deliveries", "pr_outcomes",
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
