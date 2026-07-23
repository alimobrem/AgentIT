"""``DeliveriesMixin`` -- the ``deliveries`` table (every change set routed
through ``portal/delivery.py::route_and_deliver()``), plus the two small
delivery-pipeline concurrency primitives that exist specifically to keep
that table's writes from racing: webhook-delivery dedup (``processed_
webhooks``) and the per-app delivery mutex (``delivery_locks``).

The latter two are included here rather than split into their own modules
because they exist for, and are only ever used around, this exact table's
writes -- ``claim_webhook()`` closes a race in *whether* a delivery should
be processed at all (duplicate webhook redelivery), and ``claim_delivery_
lock()``/``release_delivery_lock()`` close a race in two *overlapping*
deliveries for the same app clobbering each other's commit -- both are
delivery-pipeline concurrency concerns, not independent domains of their
own.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import asyncpg

from ._shared import _affected, _delivery_row_to_dict, _now


class DeliveriesMixin:
    _pool: asyncpg.Pool

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

        Also requires at least one outcome ``pr_url``: partial/failed
        deliveries that never opened a PR must not sit in the
        "Awaiting verification" badge queue (nothing to verify on push).
        """
        rows = await self._pool.fetch(
            """
            SELECT * FROM deliveries
            WHERE app_name = $1
              AND finding_resolution IS NULL
              AND target_findings_json != '[]'::jsonb
              AND EXISTS (
                SELECT 1
                FROM jsonb_each(COALESCE(details_json->'outcomes', '{}'::jsonb))
                     AS outcome(category, value)
                WHERE COALESCE(value->>'pr_url', '') <> ''
              )
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

    async def release_webhook_claim(self, delivery_id: str) -> None:
        """Drop a prior ``claim_webhook`` so the same delivery_id can retry.

        Used when the portal fails soft before doing real work (e.g. assess
        concurrency slot busy → HTTP 503). Without this, GitHub's automatic
        redelivery of the same ``X-GitHub-Delivery`` would hit the duplicate
        short-circuit forever and never reassess.
        """
        await self._pool.execute(
            "DELETE FROM processed_webhooks WHERE delivery_id = $1",
            delivery_id,
        )

    async def claim_delivery_lock(self, lock_key: str, stale_after_seconds: int = 1800) -> bool:
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
        block that app's deliveries forever: 1800s mirrors
        ``reap_orphaned_jobs()``'s staleness-recovery window (above
        onboard generation 600s + auto-delivery 600s ≈ 1200s worst case),
        the established precedent for "how long can a real in-progress
        operation take before assuming the process that started it is
        gone."

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
