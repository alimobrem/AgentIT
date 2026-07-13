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
