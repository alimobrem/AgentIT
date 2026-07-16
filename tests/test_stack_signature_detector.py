from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from src.agentit.stack_signature_detector import (
    detect_repeated_stacks, detect_repeated_stack_patterns, maybe_trigger_learn_for
)

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=40)


def make(stack, ts=None):
    return {"stack": stack, "timestamp": ts or NOW}


def test_below_threshold():
    a = [make("Go+Postgres") for _ in range(2)]
    assert detect_repeated_stacks(a, threshold=3) == []


def test_above_threshold():
    a = [make("Go+Postgres") for _ in range(4)]
    assert detect_repeated_stacks(a, threshold=3) == ["Go+Postgres"]


def test_known_stacks_excluded():
    a = [make("Go+Postgres") for _ in range(4)]
    assert detect_repeated_stacks(a, threshold=3, known_stacks={"Go+Postgres"}) == []


def test_maybe_trigger_calls_once_per_stack():
    fn = MagicMock()
    maybe_trigger_learn_for(["Go+Postgres"], fn)
    fn.assert_called_once_with("Go+Postgres")


def test_windowed_above_threshold():
    a = [make("Go+PostgreSQL") for _ in range(4)]
    assert detect_repeated_stack_patterns(a) == ["Go+PostgreSQL"]


def test_windowed_below_threshold():
    a = [make("Go+PostgreSQL") for _ in range(2)]
    assert detect_repeated_stack_patterns(a) == []


def test_windowed_old_excluded():
    a = [make("Go+PostgreSQL", OLD) for _ in range(3)] + [make("Go+PostgreSQL")]
    assert detect_repeated_stack_patterns(a) == []
