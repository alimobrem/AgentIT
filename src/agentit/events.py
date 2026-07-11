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


def get_kafka_stats(bootstrap: str | None = None) -> dict:
    """Return topic partition counts, end offsets, and consumer group lag."""
    bs = bootstrap or os.environ.get("AGENTIT_KAFKA_BOOTSTRAP")
    if not bs:
        return {"available": False, "topics": {}, "consumer_groups": []}

    try:
        from kafka import KafkaConsumer as _KC
        from kafka.admin import KafkaAdminClient

        admin = KafkaAdminClient(bootstrap_servers=bs, client_id="agentit-stats")
        topics_meta = admin.list_topics()
        topic_details: dict[str, dict] = {}

        consumer = _KC(bootstrap_servers=bs, consumer_timeout_ms=1000)
        for topic in topics_meta:
            if topic.startswith("__"):
                continue
            partitions = consumer.partitions_for_topic(topic)
            if not partitions:
                topic_details[topic] = {"partitions": 0, "end_offset": 0}
                continue
            from kafka import TopicPartition as _TP
            tps = [_TP(topic, p) for p in partitions]
            end_offsets = consumer.end_offsets(tps)
            total_end = sum(end_offsets.values())
            topic_details[topic] = {
                "partitions": len(partitions),
                "end_offset": total_end,
            }
        consumer.close()

        groups: list[dict] = []
        try:
            group_list = admin.list_consumer_groups()
            for g_name, _ in group_list:
                if not g_name.startswith("agentit"):
                    continue
                try:
                    offsets = admin.list_consumer_group_offsets(g_name)
                    committed = sum(o.offset for o in offsets.values() if o.offset >= 0)
                    end_total = 0
                    for tp in offsets:
                        if tp.topic in topic_details:
                            end_total += topic_details[tp.topic]["end_offset"]
                    groups.append({
                        "group": g_name,
                        "committed_offset": committed,
                        "end_offset": end_total,
                        "lag": max(0, end_total - committed),
                    })
                except Exception:
                    groups.append({"group": g_name, "lag": -1})
        except Exception:
            logger.debug("Failed to list consumer groups", exc_info=True)

        admin.close()
        return {"available": True, "topics": topic_details, "consumer_groups": groups}
    except Exception as exc:
        logger.debug("Failed to collect Kafka stats: %s", exc)
        return {"available": False, "topics": {}, "consumer_groups": []}


_publisher: EventPublisher | None = None


def get_publisher() -> EventPublisher:
    global _publisher
    if _publisher is None:
        _publisher = EventPublisher()
    return _publisher
