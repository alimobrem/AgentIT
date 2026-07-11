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
    """

    def __init__(
        self,
        topics: list[str],
        group_id: str,
        bootstrap_servers: str | None = None,
    ) -> None:
        self._topics = topics
        self._group_id = group_id
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
                enable_auto_commit=True,
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

    def poll_once(self) -> list[dict]:
        """Poll for available events. Returns list of event dicts."""
        if self._consumer is None:
            return []
        events: list[dict] = []
        try:
            for msg in self._consumer:
                events.append(msg.value)
        except Exception as exc:
            logger.warning("Kafka poll error: %s", exc)
        return events

    def consume(self, handler: Callable[[dict], None]) -> None:
        """Blocking consume loop. Calls handler for each event.

        If Kafka is unavailable, returns immediately (caller should
        fall back to time-based polling).
        """
        if self._consumer is None:
            logger.info("No Kafka consumer — skipping event consumption")
            return

        logger.info("Starting consume loop on %s", self._topics)
        try:
            for msg in self._consumer:
                try:
                    handler(msg.value)
                except Exception as exc:
                    logger.warning("Event handler error: %s", exc)
        except KeyboardInterrupt:
            logger.info("Consumer stopped by interrupt")
        finally:
            if self._consumer:
                self._consumer.close()

    def close(self) -> None:
        if self._consumer:
            self._consumer.close()
            self._consumer = None
