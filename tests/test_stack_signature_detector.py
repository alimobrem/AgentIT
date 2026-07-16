from datetime import datetime, timedelta, timezone
from agentit.stack_signature_detector import detect_repeated_stack_patterns

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=40)


def make(stack, ts=None):
    return {"stack": stack, "timestamp": ts or NOW}


def test_above_threshold():
    a = [make("Go+PostgreSQL") for _ in range(4)]
    assert detect_repeated_stack_patterns(a) == ["Go+PostgreSQL"]


def test_below_threshold():
    a = [make("Go+PostgreSQL") for _ in range(2)]
    assert detect_repeated_stack_patterns(a) == []


def test_old_excluded():
    a = [make("Go+PostgreSQL", OLD) for _ in range(3)] + [make("Go+PostgreSQL")]
    assert detect_repeated_stack_patterns(a) == []


def test_empty():
    assert detect_repeated_stack_patterns([]) == []


def test_mixed_stacks():
    a = [make("Go+PostgreSQL") for _ in range(3)] + [make("Python+Redis") for _ in range(2)]
    result = detect_repeated_stack_patterns(a)
    assert "Go+PostgreSQL" in result
    assert "Python+Redis" not in result
