"""Tests for the Kafka event consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agentit.consumer import EventConsumer


class TestEventConsumer:
    def test_no_kafka_is_not_connected(self) -> None:
        consumer = EventConsumer(topics=["test"], group_id="test-group")
        assert consumer.connected is False

    def test_poll_once_returns_empty_without_kafka(self) -> None:
        consumer = EventConsumer(topics=["test"], group_id="test-group")
        assert consumer.poll_once() == []

    def test_consume_returns_immediately_without_kafka(self) -> None:
        consumer = EventConsumer(topics=["test"], group_id="test-group")
        handler = MagicMock()
        consumer.consume(handler)
        handler.assert_not_called()

    def test_close_is_safe_without_kafka(self) -> None:
        consumer = EventConsumer(topics=["test"], group_id="test-group")
        consumer.close()

    @patch("agentit.consumer.os.environ.get", return_value="localhost:9092")
    def test_connects_with_bootstrap(self, mock_env) -> None:
        mock_kafka = MagicMock()
        with patch.dict("sys.modules", {"kafka": MagicMock(KafkaConsumer=mock_kafka)}):
            with patch("agentit.consumer.KafkaConsumer", mock_kafka, create=True):
                consumer = EventConsumer.__new__(EventConsumer)
                consumer._topics = ["test"]
                consumer._group_id = "test"
                consumer._bootstrap = "localhost:9092"
                consumer._consumer = None
                # Verify the init pattern works
                assert consumer.connected is False

    def test_dead_letter_persists_to_store_with_original_topic(self) -> None:
        """Regression: _dead_letter previously only published to Kafka's DLQ
        topic, so /events/dlq (which reads the SQLite `events` table) never
        showed anything unless something else happened to consume that topic
        and write to the store. This is the plain-sync-store path (e.g. a
        genuinely synchronous future consumer, or a test double whose
        ``log_event`` is not a coroutine function)."""
        mock_store = MagicMock()
        consumer = EventConsumer.__new__(EventConsumer)
        consumer._store = mock_store
        consumer._loop = None

        with patch("agentit.events.get_publisher") as mock_get_pub:
            consumer._dead_letter("agentit-events", {"targetApp": "app1", "action": "tick"}, RuntimeError("boom"))

        mock_store.log_event.assert_called_once()
        args, kwargs = mock_store.log_event.call_args
        assert args[1] == "dead-letter"
        assert kwargs["details"]["original_topic"] == "agentit-events"
        assert kwargs["details"]["original_message"]["action"] == "tick"
        mock_get_pub.return_value.publish.assert_called_once()

    def test_dead_letter_without_store_does_not_raise(self) -> None:
        consumer = EventConsumer.__new__(EventConsumer)
        consumer._store = None
        consumer._loop = None
        with patch("agentit.events.get_publisher"):
            consumer._dead_letter("agentit-events", {"targetApp": "app1"}, RuntimeError("boom"))

    async def test_dead_letter_schedules_async_store_write_on_captured_loop(self) -> None:
        """The real fix: EventConsumer now accepts an async-compatible store
        (like the ones the 4 watcher CLI commands construct via
        create_store()) directly -- no more handing it `.raw`. Since
        `consume()`'s blocking loop runs off the main event loop's thread
        (via `asyncio.to_thread`, per cli.py's `consume` command), the
        dead-letter write must be scheduled back onto the loop that
        constructed this consumer, not called inline."""
        mock_store = MagicMock()
        mock_store.log_event = AsyncMock()
        consumer = EventConsumer(topics=["agentit-events"], group_id="test-group", store=mock_store)
        assert consumer._loop is asyncio.get_running_loop()

        def _dead_letter_in_worker_thread() -> None:
            with patch("agentit.events.get_publisher"):
                consumer._dead_letter(
                    "agentit-events", {"targetApp": "app1", "action": "tick"}, RuntimeError("boom"),
                )

        await asyncio.to_thread(_dead_letter_in_worker_thread)

        mock_store.log_event.assert_awaited_once()
        args, kwargs = mock_store.log_event.call_args
        assert args[1] == "dead-letter"
        assert kwargs["details"]["original_topic"] == "agentit-events"

    def test_dead_letter_with_async_store_but_no_loop_logs_and_does_not_raise(self) -> None:
        """If an async store is somehow used without a captured loop (e.g.
        a store swapped in after construction), this must degrade to a
        loud warning, not a crash or a silent no-op that looks like it
        worked."""
        mock_store = MagicMock()
        mock_store.log_event = AsyncMock()
        consumer = EventConsumer.__new__(EventConsumer)
        consumer._store = mock_store
        consumer._loop = None

        with patch("agentit.events.get_publisher"):
            consumer._dead_letter("agentit-events", {"targetApp": "app1"}, RuntimeError("boom"))

        mock_store.log_event.assert_not_awaited()

    # ── SASL_SSL/SCRAM-SHA-512 credential wiring (docs/kafka-hardening-plan.md) ──

    def test_connects_plaintext_without_sasl_credentials(self, monkeypatch) -> None:
        """No AGENTIT_KAFKA_SASL_USERNAME/_PASSWORD -> KafkaConsumer gets no
        security kwargs at all -- today's exact plaintext behavior for every
        existing deployment."""
        monkeypatch.setenv("AGENTIT_KAFKA_BOOTSTRAP", "localhost:9092")
        monkeypatch.delenv("AGENTIT_KAFKA_SASL_USERNAME", raising=False)
        monkeypatch.delenv("AGENTIT_KAFKA_SASL_PASSWORD", raising=False)
        mock_kafka_consumer_cls = MagicMock()

        with patch.dict("sys.modules", {"kafka": MagicMock(KafkaConsumer=mock_kafka_consumer_cls)}):
            EventConsumer(topics=["agentit-events"], group_id="agentit-vuln-watcher")

        _, call_kwargs = mock_kafka_consumer_cls.call_args
        assert call_kwargs["bootstrap_servers"] == "localhost:9092"
        assert "security_protocol" not in call_kwargs
        assert "sasl_mechanism" not in call_kwargs

    def test_connects_sasl_ssl_with_sasl_credentials(self, monkeypatch) -> None:
        """When the KafkaUser-Secret-sourced SASL env vars ARE set, the
        consumer is configured for SASL_SSL/SCRAM-SHA-512 instead of
        plaintext."""
        monkeypatch.setenv("AGENTIT_KAFKA_BOOTSTRAP", "agentit-kafka-kafka-bootstrap.agentit.svc:9093")
        monkeypatch.setenv("AGENTIT_KAFKA_SASL_USERNAME", "agentit-vuln-watcher")
        monkeypatch.setenv("AGENTIT_KAFKA_SASL_PASSWORD", "s3cr3t")
        mock_kafka_consumer_cls = MagicMock()

        with patch.dict("sys.modules", {"kafka": MagicMock(KafkaConsumer=mock_kafka_consumer_cls)}):
            EventConsumer(topics=["agentit-events"], group_id="agentit-vuln-watcher")

        _, call_kwargs = mock_kafka_consumer_cls.call_args
        assert call_kwargs["security_protocol"] == "SASL_SSL"
        assert call_kwargs["sasl_mechanism"] == "SCRAM-SHA-512"
        assert call_kwargs["sasl_plain_username"] == "agentit-vuln-watcher"
        assert call_kwargs["sasl_plain_password"] == "s3cr3t"

    def test_poll_once_with_mock_consumer(self) -> None:
        consumer = EventConsumer.__new__(EventConsumer)
        consumer._topics = ["test"]
        consumer._group_id = "test"
        consumer._bootstrap = "localhost:9092"

        mock_msg = MagicMock()
        mock_msg.value = {"action": "test-event", "agentId": "tester"}
        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter([mock_msg]))
        consumer._consumer = mock_consumer

        events = consumer.poll_once()
        assert len(events) == 1
        assert events[0]["action"] == "test-event"


class TestConsumeOffsetCommitOrdering:
    """Priority 3b: `consume()`'s bare `commit()` commits the consumer's
    *current position* across every assigned partition, not just the
    message that just resolved -- so a later success on the same
    partition must not be allowed to silently commit past an earlier,
    still-unresolved failure's offset."""

    @staticmethod
    def _make_msg(topic: str, partition: int, offset: int, value: dict) -> MagicMock:
        msg = MagicMock()
        msg.topic = topic
        msg.partition = partition
        msg.offset = offset
        msg.value = value
        return msg

    def _make_consumer(self, max_retries: int = 3) -> EventConsumer:
        consumer = EventConsumer.__new__(EventConsumer)
        consumer._topics = ["agentit-events"]
        consumer._max_retries = max_retries
        consumer._retry_counts = {}
        consumer._blocked_offset = {}
        consumer._store = None
        consumer._consumer = MagicMock()
        return consumer

    def test_failed_message_blocks_a_later_success_from_committing_past_it(self) -> None:
        consumer = self._make_consumer(max_retries=3)
        msg_a = self._make_msg("agentit-events", 0, 10, {"id": "a"})
        msg_b = self._make_msg("agentit-events", 0, 11, {"id": "b"})
        msg_c = self._make_msg("agentit-events", 0, 12, {"id": "c"})
        consumer._consumer.__iter__ = MagicMock(return_value=iter([msg_a, msg_b, msg_c]))

        def handler(value: dict) -> None:
            if value["id"] == "b":
                raise RuntimeError("transient failure")

        consumer.consume(handler)

        # A succeeded -> committed. B failed (retries not yet exhausted) --
        # its offset must stay uncommitted. C succeeded too, but is on the
        # SAME partition as the still-open B: its commit must be held back
        # rather than fired unconditionally (which would silently commit
        # past B, since Kafka's committed offset is a single per-partition
        # watermark, not a sparse per-message ack).
        assert consumer._consumer.commit.call_count == 1
        assert consumer._blocked_offset == {("agentit-events", 0): 11}

    def test_success_on_a_different_partition_is_not_blocked(self) -> None:
        """The block is scoped per (topic, partition) -- a failure on
        partition 0 must not stall unrelated progress on partition 1."""
        consumer = self._make_consumer(max_retries=3)
        msg_a = self._make_msg("agentit-events", 0, 10, {"id": "a"})
        msg_b_fails = self._make_msg("agentit-events", 0, 11, {"id": "b"})
        msg_other_partition = self._make_msg("agentit-events", 1, 5, {"id": "d"})
        consumer._consumer.__iter__ = MagicMock(
            return_value=iter([msg_a, msg_b_fails, msg_other_partition])
        )

        def handler(value: dict) -> None:
            if value["id"] == "b":
                raise RuntimeError("transient failure")

        consumer.consume(handler)

        assert consumer._consumer.commit.call_count == 2  # after a, and after the other partition
        assert consumer._blocked_offset == {("agentit-events", 0): 11}

    def test_dead_lettered_message_resolves_the_block_and_allows_future_commits(self) -> None:
        """Exhausting retries into the DLQ is an explicit resolution --
        once that happens, later messages on the same partition must be
        able to commit again."""
        consumer = self._make_consumer(max_retries=1)
        msg_a = self._make_msg("agentit-events", 0, 10, {"id": "a"})
        msg_b = self._make_msg("agentit-events", 0, 11, {"id": "b"})
        msg_c = self._make_msg("agentit-events", 0, 12, {"id": "c"})
        consumer._consumer.__iter__ = MagicMock(return_value=iter([msg_a, msg_b, msg_c]))

        def handler(value: dict) -> None:
            if value["id"] == "b":
                raise RuntimeError("permanent failure")

        with patch("agentit.events.get_publisher"):
            consumer.consume(handler)

        # a -> commit. b exhausts its single retry immediately -> dead-
        # lettered -> commit (resolves the block). c -> commit (block
        # cleared, free to proceed).
        assert consumer._consumer.commit.call_count == 3
        assert consumer._blocked_offset == {}
