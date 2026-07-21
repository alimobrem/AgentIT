"""``AssessmentsMixin`` -- the ``assessments``/``apps``/``onboarding_results``/
``apply_results`` tables: the core assessment-run entity, the app-level
facts row that persists across re-assessments, the manifests an onboarding
run generated for one assessment, and the Dry Run results recorded against
them.

Grouped together (rather than split further) because they all key directly
off one assessment run or the app it belongs to, and several methods here
already call each other across this exact boundary (``save()`` calls
``_upsert_app()``/``_last_known_infra_repo_url()``; ``delete()`` cascades
into ``onboarding_results``/``apply_results`` for the same app) -- splitting
them into separate mixins would not remove any real coupling, just hide it
behind an extra file boundary. ``list_history()``/``get_trend()``/
``get_score_history()`` (per-app assessment history/trend) are included
here for the same reason: they read the same ``assessments`` rows this
mixin already owns.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py`` -- see that module's
docstring for the full mixin-composition contract.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

import asyncpg

from agentit.models import AssessmentReport

from ._shared import (
    ASSESSMENT_CADENCE_INTERVALS,
    ASSESSMENT_CADENCES,
    _affected,
    _now,
    _row_to_dict,
    _rows_to_dicts,
    normalize_repo_url,
)

logger = logging.getLogger(__name__)


class AssessmentsMixin:
    _pool: asyncpg.Pool

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

    async def clear_apply_results(self, assessment_id: str) -> None:
        """Delete every persisted ``apply_results`` row for this assessment.

        Called when a generated file is edited (``update_onboarding_file``)
        after a Dry Run (or a real delivery) already ran against the
        PRE-edit content -- without this, ``get_apply_results()`` keeps
        returning that stale pass/fail/delivered row, so
        ``onboard_results.html``'s ``dry_run_done``/Apply-Commit gate stays
        unlocked for content that was never actually dry-run. A no-op if
        none exist yet. The next real Dry Run (against the current,
        edited content) inserts a fresh row via ``save_apply_results``.
        """
        await self._pool.execute(
            "DELETE FROM apply_results WHERE assessment_id = $1", assessment_id,
        )

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
        # Normalize once, in place, so every downstream use of `report.repo_url`
        # in THIS call (the INSERT below, `_upsert_app`) and in the caller
        # (e.g. `assess_submit`'s `list_history(report.repo_url)`) all see the
        # same canonical string -- see `normalize_repo_url()` for why this
        # matters for `get_fleet_data()`'s `GROUP BY repo_url`.
        report.repo_url = normalize_repo_url(report.repo_url)
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

    async def get_assessment_cadence(self, repo_url: str) -> str:
        """This app's configured automatic-re-assessment cadence --
        ``'daily'`` (the schema default) if the app has no row yet, e.g. a
        repo referenced before its first assessment ever completed."""
        row = await self._pool.fetchrow(
            "SELECT assessment_cadence FROM apps WHERE repo_url = $1", repo_url,
        )
        return row["assessment_cadence"] if row is not None else "daily"

    async def set_assessment_cadence(self, repo_url: str, cadence: str) -> bool:
        """Sets how often ``watchers/reassess_scheduler.py`` should
        automatically re-Assess this app. Raises ``ValueError`` for any
        cadence outside ``ASSESSMENT_CADENCES`` -- callers (the portal
        route) are expected to have already validated user input against
        that same tuple, so this is a defensive backstop, not the primary
        validation.
        """
        if cadence not in ASSESSMENT_CADENCES:
            raise ValueError(
                f"Invalid assessment cadence {cadence!r} (must be one of {ASSESSMENT_CADENCES})"
            )
        result = await self._pool.execute(
            "UPDATE apps SET assessment_cadence = $1, updated_at = $2 WHERE repo_url = $3",
            cadence, _now(), repo_url,
        )
        return _affected(result) > 0

    async def get_apps_due_for_reassessment(self) -> list[dict]:
        """Apps whose configured ``assessment_cadence`` interval has
        elapsed since their most recent assessment -- the real, DB-backed
        query ``watchers/reassess_scheduler.py``'s tick loop uses to decide
        which apps to automatically re-Assess. Apps on the ``'manual'``
        cadence are always excluded (that's the opt-out).

        The due/not-due comparison itself happens in Python against
        ``ASSESSMENT_CADENCE_INTERVALS`` rather than as inline SQL interval
        literals, so the two stay impossible to drift apart -- there is
        exactly one place (that dict) where "weekly means 7 days" is
        decided.
        """
        rows = await self._pool.fetch(
            """
            SELECT apps.repo_url, apps.repo_name, apps.assessment_cadence,
                   latest.assessed_at AS last_assessed_at, latest.criticality
            FROM apps
            INNER JOIN (
                SELECT repo_url, MAX(assessed_at) AS max_at
                FROM assessments GROUP BY repo_url
            ) newest ON newest.repo_url = apps.repo_url
            INNER JOIN assessments latest
                ON latest.repo_url = newest.repo_url AND latest.assessed_at = newest.max_at
            WHERE apps.assessment_cadence != 'manual'
            """
        )
        now = _now()
        due = [
            r for r in rows
            if now - r["last_assessed_at"] >= ASSESSMENT_CADENCE_INTERVALS.get(
                r["assessment_cadence"], timedelta(days=999999),
            )
        ]
        due.sort(key=lambda r: r["last_assessed_at"])
        return _rows_to_dicts(due)

    async def list_all(self) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT id, repo_name, repo_url, assessed_at, overall_score, criticality
            FROM assessments ORDER BY assessed_at DESC
            """
        )
        return _rows_to_dicts(rows)

    async def delete(self, assessment_id: str) -> bool:
        """Delete the whole app, not just the one ``assessment_id`` passed
        in -- Fleet's confirm dialog (fleet.html) promises removing "ALL
        related data (assessments, onboarding, deliveries, SLOs)"
        and that this "cannot be undone". An app re-assessed more than once
        has older ``assessments`` rows the caller never names, and a delete
        scoped to only one exact id left every one of THOSE rows'
        slos/onboarding/apply_results/deliveries/events fully
        intact -- ``get_fleet_data()``'s ``MAX(assessed_at)`` join then
        picked the next-latest surviving assessment on the next Fleet load,
        silently resurrecting the "deleted" app.

        Scoped by the app's ``repo_url`` -- the same identity
        ``get_fleet_data()``/``list_history()``/``list_deliveries_for_app()``
        already key every other fleet-wide/app-wide query on (see
        docs/architecture.md's "Data model: assessments vs. apps") -- so
        every historical assessment for this app, and everything hanging
        off any of them, is removed together. ``pr_outcomes``/
        ``agent_feedback``/``skill_effectiveness`` are deliberately NOT
        included -- durable learning history that must outlive an app being
        removed from the fleet (see ``pr_outcomes`` table's own schema
        comment).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                repo_row = await conn.fetchrow(
                    "SELECT repo_url, repo_name FROM assessments WHERE id = $1", assessment_id,
                )
                if repo_row is None:
                    return False
                repo_url = repo_row["repo_url"]
                repo_name = repo_row["repo_name"]

                for table in (
                    "remediation_jobs", "onboarding_results",
                    "slos", "apply_results", "deliveries",
                ):
                    await conn.execute(
                        f"DELETE FROM {table} WHERE assessment_id IN "
                        "(SELECT id FROM assessments WHERE repo_url = $1)",
                        repo_url,
                    )
                # `events` has no `assessment_id` column/FK (see SCHEMA_SQL)
                # -- scoped instead by this app's name (most events) or, for
                # events correlated to one specific assessment run
                # (assessment-complete, onboarding-complete, ...), by
                # `correlation_id` matching one of its assessment ids.
                await conn.execute(
                    """
                    DELETE FROM events WHERE target_app = $1 OR correlation_id IN (
                        SELECT id FROM assessments WHERE repo_url = $2
                    )
                    """,
                    repo_name, repo_url,
                )
                status = await conn.execute("DELETE FROM assessments WHERE repo_url = $1", repo_url)
        return _affected(status) > 0

    async def dedupe_repo_urls(self) -> list[dict[str, str]]:
        """Self-heals any repo genuinely represented under two (or more)
        different raw ``repo_url`` spellings that ``normalize_repo_url()``
        would collapse to one -- a `.git` suffix, a trailing slash, ... --
        by merging every non-canonical variant into the canonical
        (normalized) form. The defense-in-depth complement to
        ``SCHEMA_SQL``'s ``normalize_repo_url_before_write`` trigger: that
        trigger stops any *future* write from landing a non-normalized
        value, but can't retroactively fix rows written before it existed
        -- exactly the real incident this backs up (a Tekton
        ``register-self-in-fleet`` step posted a hardcoded `.git`-suffixed
        ``repo-url`` before ``normalize_repo_url()`` was live, briefly
        creating a second Fleet row for AgentIT itself that needed a
        manual DB cleanup to remove).

        Called once from ``create()`` (so a fresh deploy heals whatever it
        inherited immediately) and periodically from the background
        maintenance loop (``app.py::_background_maintenance``, alongside
        ``reap_orphaned_jobs()``) -- so a duplicate introduced by any other
        means self-heals too, on its own, with no one needing live DB
        access to notice or fix it by hand.

        Merges rather than deletes: every ``assessments`` row (and its
        dependents, all keyed by ``assessment_id`` -- unaffected by a
        ``repo_url`` change) simply changes identity, so no assessment
        history is lost; only the ``apps`` row (keyed BY ``repo_url``)
        needs an actual field-level merge -- see ``_merge_app_repo_url()``.
        Safe to call repeatedly and concurrently from multiple replicas:
        each merge is its own transaction, and by the time a second
        replica reaches the same variant it finds nothing left to move
        (Postgres's row-level locking + read-committed re-check naturally
        serializes two overlapping merges of the same row without either
        erroring). Returns every merge performed, as
        ``{"from": <variant>, "to": <canonical>}``; empty when nothing
        needed healing.
        """
        rows = await self._pool.fetch("SELECT DISTINCT repo_url FROM assessments")
        variants_by_canonical: dict[str, set[str]] = {}
        for row in rows:
            raw = row["repo_url"]
            variants_by_canonical.setdefault(normalize_repo_url(raw), set()).add(raw)

        merged: list[dict[str, str]] = []
        for canonical, variants in variants_by_canonical.items():
            for variant in sorted(variants - {canonical}):
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        # Also fix the embedded `repo_url` inside each row's
                        # `report_json` blob, not just the column -- `get()`
                        # (and every caller that does `report.repo_url` after
                        # it, e.g. `list_history(report.repo_url)`) reads
                        # that embedded value, not this column. Leaving it
                        # stale would just move this exact bug one layer
                        # down instead of fixing it.
                        await conn.execute(
                            """
                            UPDATE assessments
                            SET repo_url = $1,
                                report_json = jsonb_set(report_json, '{repo_url}', to_jsonb($1::text))
                            WHERE repo_url = $2
                            """,
                            canonical, variant,
                        )
                        await self._merge_app_repo_url(conn, variant, canonical)
                merged.append({"from": variant, "to": canonical})
                logger.warning(
                    "dedupe_repo_urls: merged non-canonical repo_url %r into %r",
                    variant, canonical,
                )
        return merged

    async def _merge_app_repo_url(self, conn: asyncpg.Connection, variant: str, canonical: str) -> None:
        """Folds the ``apps`` row (if any) for a non-canonical ``variant``
        into the canonical row. ``apps.repo_url`` is a PRIMARY KEY, so
        (unlike ``assessments``) this can't always be a bare rename -- when
        a canonical row already exists too, keeps the earliest
        ``created_at`` (first time this app was ever assessed, under any
        spelling) and the newest ``updated_at``/``infra_repo_url`` (the same
        "most recent write wins" policy ``_upsert_app`` already uses for a
        normal re-assessment).
        """
        variant_row = await conn.fetchrow("SELECT * FROM apps WHERE repo_url = $1", variant)
        if variant_row is None:
            return
        canonical_row = await conn.fetchrow("SELECT * FROM apps WHERE repo_url = $1", canonical)
        if canonical_row is None:
            await conn.execute("UPDATE apps SET repo_url = $1 WHERE repo_url = $2", canonical, variant)
            return
        newer, older = (
            (variant_row, canonical_row) if variant_row["updated_at"] > canonical_row["updated_at"]
            else (canonical_row, variant_row)
        )
        await conn.execute(
            """
            UPDATE apps SET
                infra_repo_url = COALESCE($1, $2),
                created_at = $3,
                updated_at = $4
            WHERE repo_url = $5
            """,
            newer["infra_repo_url"], older["infra_repo_url"],
            min(variant_row["created_at"], canonical_row["created_at"]),
            max(variant_row["updated_at"], canonical_row["updated_at"]),
            canonical,
        )
        await conn.execute("DELETE FROM apps WHERE repo_url = $1", variant)

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

    async def list_onboardings_for_repo(self, repo_url: str) -> list[dict]:
        """Every onboarding_results row with a real ``pr_url``, across EVERY
        historical assessment of ``repo_url`` -- not just one assessment_id.
        Joined by ``repo_url`` (not an exact ``assessment_id`` match), the
        same "apps outlive a single assessment run" convention
        ``list_deliveries_for_app()`` already uses (docs/architecture.md).
        Used by ``pr_tracking.py`` to build one app's full PR History from
        every ``source-repo-pr``/``app-repo-pr`` delivery outcome, not just
        the current assessment's own deliveries. ``onboarding_results.
        pr_url`` may itself be several ``|``-joined URLs (Per-Agent PRs
        writes multiple back into this one column, see
        ``routes/assessments.py::create_agent_prs_route``); this returns the
        raw field as-is and leaves splitting it to the caller.
        """
        rows = await self._pool.fetch(
            """
            SELECT onboarding_results.id, onboarding_results.assessment_id,
                   onboarding_results.created_at, onboarding_results.pr_url
            FROM onboarding_results
            INNER JOIN assessments ON onboarding_results.assessment_id = assessments.id
            WHERE assessments.repo_url = $1 AND onboarding_results.pr_url != ''
            ORDER BY onboarding_results.created_at DESC
            """,
            repo_url,
        )
        return [
            {"id": r["id"], "assessment_id": r["assessment_id"],
             "created_at": r["created_at"].isoformat(), "pr_url": r["pr_url"]}
            for r in rows
        ]

    async def list_all_onboarding_pr_urls(self) -> list[dict]:
        """Fleet-wide equivalent of ``list_onboardings_for_repo()`` -- every
        onboarding_results row with a real ``pr_url``, across every app, in
        one query (mirrors ``list_all_deliveries()``'s existing fleet-wide-
        in-one-query convention). Used by Fleet's
        "Total PRs"/"Open PRs" columns so that enrichment never issues one
        onboarding query per app.
        """
        rows = await self._pool.fetch(
            """
            SELECT onboarding_results.id, onboarding_results.assessment_id,
                   onboarding_results.created_at, onboarding_results.pr_url,
                   assessments.repo_url AS repo_url
            FROM onboarding_results
            INNER JOIN assessments ON onboarding_results.assessment_id = assessments.id
            WHERE onboarding_results.pr_url != ''
            ORDER BY onboarding_results.created_at DESC
            """,
        )
        return [
            {"id": r["id"], "assessment_id": r["assessment_id"], "repo_url": r["repo_url"],
             "created_at": r["created_at"].isoformat(), "pr_url": r["pr_url"]}
            for r in rows
        ]

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

    async def get_score_history(self, repo_url: str, limit: int = 20) -> list[dict]:
        """Get score history for trend visualization."""
        rows = await self._pool.fetch(
            """SELECT id, assessed_at, overall_score, criticality
               FROM assessments WHERE repo_url = $1
               ORDER BY assessed_at DESC LIMIT $2""",
            repo_url, limit,
        )
        return _rows_to_dicts(list(reversed(rows)))
