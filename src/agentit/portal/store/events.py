"""``EventsMixin`` -- the ``events`` table: the system-wide activity feed
(every agent/watcher/route action, at every severity, gets a row here) plus
its dead-letter-queue sub-view (``action = 'dead-letter'`` rows and their
retry/dismiss lifecycle).

Kept as one mixin rather than split further (e.g. "events" vs. "DLQ")
because the DLQ rows live in the exact same ``events`` table and every DLQ
method (``retry_dlq_message()``, etc.) is really just a specialized
read/update against it -- there is no separate table or independent state
to split along.

Every method assumes ``self._pool`` (an ``asyncpg.Pool``), set by
``AssessmentStore.__init__`` in ``store/__init__.py``. ``retry_dlq_message()``
also calls ``self.log_event()`` (defined in this same mixin) and
``save()``/``save_onboarding()`` in ``assessments.py`` call ``self.log_event()``
too -- the mixin-composition pattern this whole package uses means those
cross-domain calls resolve through normal attribute lookup on the combined
``AssessmentStore`` instance without either module importing the other; see
``store/__init__.py``'s module docstring for the full rationale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import asyncpg

from ._shared import _affected, _now, _row_to_dict, _rows_to_dicts

logger = logging.getLogger(__name__)


class EventsMixin:
    _pool: asyncpg.Pool

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
