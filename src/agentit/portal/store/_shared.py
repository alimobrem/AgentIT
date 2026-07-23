"""Shared, table-agnostic helpers used by every domain mixin in this package.

Nothing here holds any state of its own -- every function is a pure helper
(row-shape conversion, timestamp/weighting math) or a module-level constant
(schema DDL, table-name lists, cadence config) shared across two or more
domain mixins, or re-exported from the package's ``__init__.py`` for
external callers (``from agentit.portal.store import normalize_repo_url``,
etc. -- see that module's docstring for the full compatibility contract).

Kept centralized rather than split alongside each owning domain: several of
these (``SCHEMA_SQL``, ``_ALL_TABLES``, ``_row_to_dict``) span many tables/
domains at once, so splitting them per-domain would mean either duplicating
them or introducing cross-module import cycles for no real benefit -- see
``store/__init__.py``'s module docstring for the full "why one shared module,
not N" rationale.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg


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
    processed_at TIMESTAMPTZ NOT NULL,
    -- FALSE while background work runs; TRUE only when assess/verify
    -- finished successfully. Incomplete rows older than the claim TTL
    -- may be reclaimed so a crashed worker cannot block GitHub forever.
    completed BOOLEAN NOT NULL DEFAULT TRUE
);

ALTER TABLE processed_webhooks
    ADD COLUMN IF NOT EXISTS completed BOOLEAN NOT NULL DEFAULT TRUE;

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

-- Persisted secret-classify verdicts so repeat Scans skip LLM + Decisions
-- rows for the same (app, path, content-hash) false positive. Content hash
-- invalidates when the matched line changes (real secret rotated in).
CREATE TABLE IF NOT EXISTS secret_classify_cache (
    app_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    snippet_hash TEXT NOT NULL,
    secret_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'llm',
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    hit_count INT NOT NULL DEFAULT 1,
    PRIMARY KEY (app_name, file_path, snippet_hash)
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
    "suppressed_checks", "secret_classify_cache", "skill_inventory_snapshots",
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
