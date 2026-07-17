"""Kafka event consumer for long-lived agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable

logger = logging.getLogger(__name__)


class EventConsumer:
    """Subscribe to Kafka topics and process events.

    Falls back to no-op mode when Kafka is unavailable, allowing the
    agent to run in polling-only mode without crashing.

    Uses manual offset commits so events are only marked consumed after
    successful processing.  Failed messages are retried up to
    ``max_retries`` times before being published to a dead-letter topic.

    ``consume()`` is a genuinely synchronous, blocking loop -- ``kafka-
    python`` has no async client, so ``for msg in self._consumer`` blocks
    the calling thread for as long as the loop runs (this is one of the
    few call sites that stays synchronous by design, not an oversight).
    ``store``, however, is the same ``AssessmentStore`` every other caller
    in this codebase uses. To let ``_dead_letter`` genuinely `await` that
    store's methods from inside a synchronous call stack,
    ``EventConsumer`` captures the event loop that constructed it (via
    ``asyncio.get_running_loop()``) and schedules the store write back
    onto that *same* loop with ``asyncio.run_coroutine_threadsafe`` --
    this is the narrow bridge, not a whole-class wrapper, and it's the
    only safe way to reach an ``asyncpg``-backed store (whose connection
    pool is bound to the loop that created it and must not be driven from
    a second event loop) from a worker thread. Callers that run
    ``consume()`` itself must do so via ``asyncio.to_thread(...)`` (see
    ``cli.py``'s ``consume`` command) so the loop this bridge schedules
    onto is free to actually process the scheduled callback while the
    Kafka loop blocks a different thread.
    """

    #: Overridden per-instance in ``__init__``; kept as a class default so
    #: instances built via ``EventConsumer.__new__(EventConsumer)`` in
    #: tests (bypassing ``__init__``) still behave correctly.
    _loop: "asyncio.AbstractEventLoop | None" = None

    def __init__(
        self,
        topics: list[str],
        group_id: str = "agentit-consumers",
        bootstrap_servers: str | None = None,
        max_retries: int = 3,
        store: object | None = None,
    ) -> None:
        self._topics = topics
        self._group_id = group_id
        self._max_retries = max_retries
        self._retry_counts: dict[tuple[str, int], int] = {}  # (topic, offset) -> count
        # (topic, partition) -> lowest offset on that partition that has
        # failed and is not yet resolved (via a later success or DLQ).
        # `consume()`'s bare `commit()` commits the consumer's *current
        # position* across every assigned partition, not just the message
        # that just succeeded -- so a later, on-the-same-partition success
        # committing unconditionally would silently mark this still-open
        # failure's offset committed too. Guards against exactly that.
        self._blocked_offset: dict[tuple[str, int], int] = {}
        self._bootstrap = bootstrap_servers or os.environ.get(
            "AGENTIT_KAFKA_BOOTSTRAP", ""
        )
        self._consumer = None
        # Optional async-compatible store — when provided, dead-lettered
        # messages are also persisted to the `events` table
        # (action='dead-letter') so the portal's /events/dlq page actually
        # shows them, not just Kafka.
        self._store = store
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        if not self._bootstrap:
            logger.warning("No Kafka bootstrap servers — consumer will run in polling-only mode")
            return

        try:
            from kafka import KafkaConsumer

            from agentit.events import kafka_security_kwargs

            self._consumer = KafkaConsumer(
                *topics,
                bootstrap_servers=self._bootstrap,
                group_id=group_id,
                auto_offset_reset="latest",
                enable_auto_commit=False,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                consumer_timeout_ms=5000,
                **kafka_security_kwargs(),
            )
            logger.info("Kafka consumer connected: topics=%s group=%s", topics, group_id)
        except Exception as exc:
            logger.warning("Kafka consumer init failed (polling-only mode): %s", exc)
            self._consumer = None

    @property
    def connected(self) -> bool:
        return self._consumer is not None

    def _dead_letter(self, topic: str, message: dict, error: Exception) -> None:
        """Publish a failed message to the dead-letter topic and persist it locally.

        ``original_topic`` is stored in ``details`` alongside the original message
        so that a later retry (``store.retry_dlq_message``) knows where to
        republish it.
        """
        from agentit.events import get_publisher, TOPIC_DLQ

        details = {"original_topic": topic, "original_message": message, "error": str(error)}
        publisher = get_publisher()
        publisher.publish(
            TOPIC_DLQ,
            agent_id="event-consumer",
            action="dead-letter",
            target_app=message.get("targetApp"),
            severity="error",
            summary=f"Dead-lettered from {topic}: {error}",
            details=details,
        )
        logger.error("Dead-lettered message from %s: %s", topic, error)

        if self._store is not None:
            try:
                self._persist_dead_letter(topic, message, error, details)
            except Exception:
                logger.exception("Failed to persist dead-letter event to store")

    def _persist_dead_letter(self, topic: str, message: dict, error: Exception, details: dict) -> None:
        """Write the dead-letter event to ``self._store``, sync or async.

        Sync stores (e.g. a plain ``MagicMock`` in tests, or a future
        genuinely-sync consumer) get a direct call. Async stores are
        scheduled back onto the loop that constructed this consumer via
        ``run_coroutine_threadsafe`` and awaited to completion here, since
        this method itself runs synchronously inside ``consume()``'s
        blocking loop -- see the class docstring for why that loop must
        run off the main event loop's thread for this to work.
        """
        log_event = self._store.log_event
        args = (
            "event-consumer", "dead-letter", message.get("targetApp"), "error",
            f"Dead-lettered from {topic}: {error}",
        )
        if not asyncio.iscoroutinefunction(log_event):
            log_event(*args, details=details)
            return
        if self._loop is None:
            logger.warning(
                "Async store but no event loop captured — dropping dead-letter persistence"
            )
            return
        future = asyncio.run_coroutine_threadsafe(log_event(*args, details=details), self._loop)
        future.result(timeout=10)

    def poll_once(self) -> list[dict]:
        """Poll for available events. Returns list of event dicts."""
        if self._consumer is None:
            return []
        events: list[dict] = []
        try:
            for msg in self._consumer:
                events.append(msg.value)
            self._consumer.commit()
        except Exception as exc:
            logger.warning("Kafka poll error: %s", exc)
        return events

    def _commit_resolved(self, partition_key: tuple[str, int], offset: int) -> None:
        """Commit progress for this partition, unless an earlier,
        still-unresolved failure on the *same* partition would otherwise
        get silently marked committed too.

        `KafkaConsumer.commit()` with no explicit offsets commits the
        current position for every assigned partition (whatever's been
        fetched so far), not just the specific message that just resolved
        -- so calling it unconditionally after a later success on the
        same partition would silently commit past an earlier failure's
        offset that hasn't itself been resolved yet, undermining the
        "committed only after success or DLQ" guarantee above.
        """
        blocked = self._blocked_offset.get(partition_key)
        if blocked is not None and blocked != offset:
            # A strictly earlier offset on this partition is still
            # pending -- hold this commit rather than skip past it.
            return
        self._blocked_offset.pop(partition_key, None)
        self._consumer.commit()

    def consume(self, handler: Callable[[dict], None]) -> None:
        """Blocking consume loop. Calls handler for each event.

        If Kafka is unavailable, returns immediately (caller should
        fall back to time-based polling).

        Offsets are committed only after successful handler execution or
        after exhausting retries (message goes to DLQ) -- see
        ``_commit_resolved()`` for how a still-open failure on the same
        partition holds back a later message's commit rather than
        letting it silently skip ahead.
        """
        if self._consumer is None:
            logger.info("No Kafka consumer — skipping event consumption")
            return

        logger.info("Starting consume loop on %s", self._topics)
        try:
            for msg in self._consumer:
                retry_key = (msg.topic, msg.offset)
                partition_key = (msg.topic, msg.partition)
                try:
                    handler(msg.value)
                    self._retry_counts.pop(retry_key, None)
                    self._commit_resolved(partition_key, msg.offset)
                except Exception as exc:
                    count = self._retry_counts.get(retry_key, 0) + 1
                    self._retry_counts[retry_key] = count
                    if count >= self._max_retries:
                        logger.exception(
                            "Handler failed after %d retries for %s offset %d",
                            count, msg.topic, msg.offset,
                        )
                        self._dead_letter(msg.topic, msg.value, exc)
                        self._retry_counts.pop(retry_key, None)
                        self._commit_resolved(partition_key, msg.offset)
                    else:
                        logger.warning(
                            "Handler error (retry %d/%d) for %s offset %d: %s",
                            count, self._max_retries, msg.topic, msg.offset, exc,
                        )
                        self._blocked_offset.setdefault(partition_key, msg.offset)
        except KeyboardInterrupt:
            logger.info("Consumer stopped by interrupt")
        finally:
            if self._consumer:
                self._consumer.close()

    def close(self) -> None:
        if self._consumer:
            self._consumer.close()
            self._consumer = None
