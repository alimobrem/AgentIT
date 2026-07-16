from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Topic constants ───────────────────────────────────────────────────
TOPIC_EVENTS = "agentit-events"
TOPIC_ASSESSMENTS = "agentit-assessments"
TOPIC_GATES = "agentit-gates"
TOPIC_DECISIONS = "agentit-decisions"
TOPIC_ALERTS = "agentit-alerts"
TOPIC_DLQ = "agentit-dlq"


def kafka_security_kwargs() -> dict:
    """Build kafka-python SASL_SSL/SCRAM-SHA-512 kwargs from env vars.

    Additive/opt-in, per docs/kafka-hardening-plan.md: when
    AGENTIT_KAFKA_SASL_USERNAME/_PASSWORD aren't set (today's default for
    every existing deployment), this returns ``{}`` and both
    ``EventPublisher``/``EventConsumer`` construct their kafka-python client
    exactly as before -- plain, unauthenticated ``bootstrap_servers``. Only
    when both are present does the client get configured for SASL_SSL, with
    SCRAM-SHA-512 as the mechanism (matching the KafkaUser CRs in
    chart/templates/kafka/kafka-users.yaml). Those two env vars, plus the
    optional CA file, are meant to be sourced from a Strimzi-generated
    KafkaUser Secret -- this function only reads whatever is already in the
    process environment; it does not know or care how it got there.
    """
    username = os.environ.get("AGENTIT_KAFKA_SASL_USERNAME")
    password = os.environ.get("AGENTIT_KAFKA_SASL_PASSWORD")
    if not username or not password:
        return {}
    kwargs: dict = {
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": os.environ.get("AGENTIT_KAFKA_SASL_MECHANISM", "SCRAM-SHA-512"),
        "sasl_plain_username": username,
        "sasl_plain_password": password,
    }
    ca_file = os.environ.get("AGENTIT_KAFKA_SSL_CAFILE")
    if ca_file:
        kwargs["ssl_cafile"] = ca_file
    return kwargs


class EventPublisher:
    _RECONNECT_COOLDOWN = 60

    def __init__(self, bootstrap_servers: str | None = None) -> None:
        self._bootstrap = bootstrap_servers or os.environ.get("AGENTIT_KAFKA_BOOTSTRAP")
        self._producer = None
        self._last_reconnect: float = 0
        self._buffer_db = self._resolve_buffer_db()
        self._init_buffer_db()
        if self._bootstrap:
            self._connect()

    @staticmethod
    def _resolve_buffer_db() -> str:
        """This buffer is a local, durable disk queue for Kafka publish
        failures -- deliberately still SQLite, and deliberately unrelated
        to `AssessmentStore`/Postgres: it exists specifically so events can
        be buffered *without* depending on any network service (including
        the primary store) being reachable. Co-located under the shared
        `/data` PVC when mounted (see `chart/templates/pvc.yaml`), which
        every component that publishes events still mounts."""
        data_dir = Path("/data")
        if data_dir.is_dir():
            return str(data_dir / "event-buffer.db")
        return "event-buffer.db"

    def _init_buffer_db(self) -> None:
        conn = sqlite3.connect(self._buffer_db)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buffered_events (
                id INTEGER PRIMARY KEY,
                topic TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def _connect(self) -> None:
        try:
            from kafka import KafkaProducer

            self._producer = KafkaProducer(
                bootstrap_servers=self._bootstrap,
                value_serializer=lambda v: json.dumps(v).encode(),
                acks="all",
                **kafka_security_kwargs(),
            )
            self._drain_buffer()
        except Exception as exc:
            logger.warning("Kafka unavailable: %s", exc)
            self._producer = None

    def _buffer_locally(self, topic: str, event: dict) -> None:
        try:
            conn = sqlite3.connect(self._buffer_db)
            conn.execute(
                "INSERT INTO buffered_events (topic, event_json, created_at) VALUES (?, ?, ?)",
                (topic, json.dumps(event), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            conn.close()
            logger.info("Buffered event %s/%s locally", topic, event.get("action", "?"))
        except Exception as exc:
            logger.error("Failed to buffer event locally: %s", exc)

    def _drain_buffer(self) -> None:
        if not self._producer:
            return
        try:
            conn = sqlite3.connect(self._buffer_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, topic, event_json FROM buffered_events ORDER BY id ASC"
            ).fetchall()
            if not rows:
                conn.close()
                return
            logger.info("Draining %d buffered events", len(rows))
            for row in rows:
                event = json.loads(row["event_json"])
                try:
                    self._producer.send(row["topic"], event)
                    self._producer.flush(timeout=5)
                    conn.execute("DELETE FROM buffered_events WHERE id = ?", (row["id"],))
                    conn.commit()
                except Exception as exc:
                    logger.warning("Failed to drain buffered event %d: %s", row["id"], exc)
                    break
            conn.close()
        except Exception as exc:
            logger.error("Buffer drain failed: %s", exc)

    def _try_reconnect(self) -> None:
        if not self._bootstrap:
            return
        now = time.monotonic()
        if now - self._last_reconnect < self._RECONNECT_COOLDOWN:
            return
        self._last_reconnect = now
        logger.info("Attempting Kafka reconnect...")
        self._connect()

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
        if self._producer is None:
            self._try_reconnect()
        if self._producer is None:
            self._buffer_locally(topic, event)
            return event
        try:
            self._producer.send(topic, event)
            self._producer.flush(timeout=5)
        except Exception as exc:
            logger.error("Kafka publish failed for %s/%s: %s — buffering locally",
                         topic, action, exc)
            self._buffer_locally(topic, event)
            self._producer = None
        return event

    def close(self) -> None:
        if self._producer:
            self._producer.close()

    def get_buffer_backlog(self) -> int:
        """Number of events buffered locally in event-buffer.db, pending Kafka delivery."""
        try:
            conn = sqlite3.connect(self._buffer_db)
            row = conn.execute("SELECT COUNT(*) FROM buffered_events").fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            logger.debug("Failed to read event buffer backlog", exc_info=True)
            return 0


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
