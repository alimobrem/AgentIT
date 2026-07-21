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

from .agents import AgentsMixin
from .assessments import AssessmentsMixin
from .checks import ChecksMixin
from .deliveries import DeliveriesMixin
from .events import EventsMixin
from .feedback import FeedbackMixin
from .fleet import FleetMixin
from .jobs import JobsMixin
from .schedules import SchedulesMixin
from .slos import SLOsMixin

logger = logging.getLogger(__name__)

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


class AssessmentStore(
    AssessmentsMixin, EventsMixin, FleetMixin, DeliveriesMixin, AgentsMixin, SLOsMixin, JobsMixin,
    SchedulesMixin, FeedbackMixin, ChecksMixin,
):
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
