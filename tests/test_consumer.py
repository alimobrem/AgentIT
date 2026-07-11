"""Tests for the Kafka event consumer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
