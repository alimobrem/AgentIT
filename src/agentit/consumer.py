"""Kafka event consumer for long-lived agents."""

from __future__ import annotations

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
    """

    def __init__(
        self,
        topics: list[str],
        group_id: str = "agentit-consumers",
        bootstrap_servers: str | None = None,
        max_retries: int = 3,
    ) -> None:
        self._topics = topics
        self._group_id = group_id
        self._max_retries = max_retries
        self._retry_counts: dict[tuple[str, int], int] = {}  # (topic, offset) -> count
        self._bootstrap = bootstrap_servers or os.environ.get(
            "AGENTIT_KAFKA_BOOTSTRAP", ""
        )
        self._consumer = None

        if not self._bootstrap:
            logger.warning("No Kafka bootstrap servers — consumer will run in polling-only mode")
            return

        try:
            from kafka import KafkaConsumer
            self._consumer = KafkaConsumer(
                *topics,
                bootstrap_servers=self._bootstrap,
                group_id=group_id,
                auto_offset_reset="latest",
                enable_auto_commit=False,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                consumer_timeout_ms=5000,
            )
            logger.info("Kafka consumer connected: topics=%s group=%s", topics, group_id)
        except Exception as exc:
            logger.warning("Kafka consumer init failed (polling-only mode): %s", exc)
            self._consumer = None

    @property
    def connected(self) -> bool:
        return self._consumer is not None

    def _dead_letter(self, topic: str, message: dict, error: Exception) -> None:
        """Publish a failed message to the dead-letter topic."""
        from agentit.events import get_publisher

        publisher = get_publisher()
        publisher.publish(
            "agentit-dlq",
            agent_id="event-consumer",
            action="dead-letter",
            target_app=message.get("targetApp"),
            severity="error",
            summary=f"Dead-lettered from {topic}: {error}",
            details={"original_message": message, "error": str(error)},
        )
        logger.error("Dead-lettered message from %s: %s", topic, error)

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

    def consume(self, handler: Callable[[dict], None]) -> None:
        """Blocking consume loop. Calls handler for each event.

        If Kafka is unavailable, returns immediately (caller should
        fall back to time-based polling).

        Offsets are committed only after successful handler execution or
        after exhausting retries (message goes to DLQ).
        """
        if self._consumer is None:
            logger.info("No Kafka consumer — skipping event consumption")
            return

        logger.info("Starting consume loop on %s", self._topics)
        try:
            for msg in self._consumer:
                retry_key = (msg.topic, msg.offset)
                try:
                    handler(msg.value)
                    self._consumer.commit()
                    self._retry_counts.pop(retry_key, None)
                except Exception as exc:
                    count = self._retry_counts.get(retry_key, 0) + 1
                    self._retry_counts[retry_key] = count
                    if count >= self._max_retries:
                        logger.exception(
                            "Handler failed after %d retries for %s offset %d",
                            count, msg.topic, msg.offset,
                        )
                        self._dead_letter(msg.topic, msg.value, exc)
                        self._consumer.commit()
                        self._retry_counts.pop(retry_key, None)
                    else:
                        logger.warning(
                            "Handler error (retry %d/%d) for %s offset %d: %s",
                            count, self._max_retries, msg.topic, msg.offset, exc,
                        )
        except KeyboardInterrupt:
            logger.info("Consumer stopped by interrupt")
        finally:
            if self._consumer:
                self._consumer.close()

    def close(self) -> None:
        if self._consumer:
            self._consumer.close()
            self._consumer = None
