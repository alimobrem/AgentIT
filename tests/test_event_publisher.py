from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.events import EventPublisher


def test_publish_without_kafka():
    """No bootstrap servers → event returned, no Kafka interaction."""
    pub = EventPublisher(bootstrap_servers=None)
    assert not pub.kafka_enabled
    event = pub.publish("test-topic", "agent-1", "deploy", summary="ok")
    assert "eventId" in event
    assert event["agentId"] == "agent-1"
    assert event["action"] == "deploy"


def test_publish_with_mock_kafka():
    """Mock KafkaProducer, verify send called with correct topic and envelope."""
    pub = EventPublisher(bootstrap_servers=None)
    mock_producer = MagicMock()
    pub._producer = mock_producer

    assert pub.kafka_enabled

    event = pub.publish("ops-events", "agent-2", "rollback", target_app="myapp")

    mock_producer.send.assert_called_once()
    call_args = mock_producer.send.call_args
    assert call_args[0][0] == "ops-events"
    payload = call_args[0][1]
    assert payload["agentId"] == "agent-2"
    assert payload["action"] == "rollback"
    assert payload["targetApp"] == "myapp"
    mock_producer.flush.assert_called_once_with(timeout=5)


def test_event_envelope_schema():
    """Verify all required fields present in event envelope."""
    pub = EventPublisher()
    event = pub.publish(
        "t",
        agent_id="a",
        action="scan",
        target_app="app",
        severity="warning",
        summary="found issues",
        details={"count": 3},
        correlation_id="corr-1",
    )
    required = {
        "eventId",
        "timestamp",
        "agentId",
        "action",
        "targetApp",
        "severity",
        "result",
        "correlationId",
    }
    assert required.issubset(event.keys())
    assert event["result"]["status"] == "success"
    assert event["result"]["summary"] == "found issues"
    assert event["result"]["details"] == {"count": 3}
    assert event["correlationId"] == "corr-1"


def test_kafka_failure_graceful():
    """producer.send raises → no exception propagated."""
    pub = EventPublisher()
    mock_producer = MagicMock()
    mock_producer.send.side_effect = RuntimeError("broker down")
    pub._producer = mock_producer

    event = pub.publish("t", "a", "x")
    assert "eventId" in event  # event still returned


def test_event_id_is_unique():
    """Two publishes produce distinct eventId values."""
    pub = EventPublisher(bootstrap_servers=None)
    e1 = pub.publish("t", "a", "x")
    e2 = pub.publish("t", "a", "x")
    assert e1["eventId"] != e2["eventId"]


def test_timestamp_is_iso_format():
    """Timestamp parses as valid ISO-8601."""
    from datetime import datetime

    pub = EventPublisher(bootstrap_servers=None)
    event = pub.publish("t", "a", "x")
    # Will raise ValueError if not valid ISO format
    datetime.fromisoformat(event["timestamp"])


def test_severity_defaults_to_info():
    """Severity defaults to 'info' when not specified."""
    pub = EventPublisher(bootstrap_servers=None)
    event = pub.publish("t", "a", "x")
    assert event["severity"] == "info"


def test_details_default_to_empty_dict():
    """Details default to empty dict when not specified."""
    pub = EventPublisher(bootstrap_servers=None)
    event = pub.publish("t", "a", "x")
    assert event["result"]["details"] == {}


def test_dual_write_kafka_and_return():
    """Event is both sent to Kafka and returned to the caller."""
    pub = EventPublisher(bootstrap_servers=None)
    mock_producer = MagicMock()
    pub._producer = mock_producer

    event = pub.publish("topic", "agent-1", "deploy", summary="done")

    mock_producer.send.assert_called_once()
    assert event["agentId"] == "agent-1"
    assert event["action"] == "deploy"


def test_kafka_flush_timeout():
    """flush raises TimeoutError → event still returned gracefully."""
    pub = EventPublisher(bootstrap_servers=None)
    mock_producer = MagicMock()
    mock_producer.flush.side_effect = TimeoutError("flush timed out")
    pub._producer = mock_producer

    event = pub.publish("t", "a", "x")
    assert "eventId" in event


def test_close_calls_producer_close():
    """close() delegates to the underlying producer."""
    pub = EventPublisher(bootstrap_servers=None)
    mock_producer = MagicMock()
    pub._producer = mock_producer

    pub.close()
    mock_producer.close.assert_called_once()
