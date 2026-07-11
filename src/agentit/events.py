from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class EventPublisher:
    def __init__(self, bootstrap_servers: str | None = None) -> None:
        self._bootstrap = bootstrap_servers or os.environ.get("AGENTIT_KAFKA_BOOTSTRAP")
        self._producer = None
        if self._bootstrap:
            self._connect()

    def _connect(self) -> None:
        try:
            from kafka import KafkaProducer

            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap,
                value_serializer=lambda v: json.dumps(v).encode(),
                acks="all",
            )
        except Exception as exc:
            logger.warning("Kafka unavailable: %s", exc)
            self._producer = None

    @property
    def kafka_enabled(self) -> bool:
        return self._producer is not None

    def publish(
        self,
        topic: str,
        agent_id: str,
        action: str,
        target_app: str | None = None,
        severity: str = "info",
        summary: str = "",
        details: dict | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        event = {
            "eventId": uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agentId": agent_id,
            "action": action,
            "targetApp": target_app,
            "severity": severity,
            "result": {
                "status": "success",
                "summary": summary,
                "details": details or {},
            },
            "correlationId": correlation_id,
        }
        if self._producer:
            try:
                self._producer.send(topic, event)
                self._producer.flush(timeout=5)
            except Exception as exc:
                logger.error("Kafka publish failed for %s/%s: %s — attempting reconnect",
                             topic, action, exc)
                self._producer = None
                self._connect()
        return event

    def close(self) -> None:
        if self._producer:
            self._producer.close()


_publisher: EventPublisher | None = None


def get_publisher() -> EventPublisher:
    global _publisher
    if _publisher is None:
        _publisher = EventPublisher()
    return _publisher
