from agentit.tick_failure_advisor import analyze_tick_failure


def test_permission_denied_returns_hint():
    event = {"summary": "tick failed: [Errno 13] Permission denied: /opt/app-root/src/tests/foo.py"}
    result = analyze_tick_failure(event)
    assert result is not None
    assert result["affected_path"] == "/opt/app-root/src/tests/foo.py"
    assert "permission" in result["hint_message"]
    assert "chmod" in result["suggested_command"]


def test_unrelated_failure_returns_none():
    assert analyze_tick_failure({"summary": "tick failed: connection timeout"}) is None


def test_missing_summary_returns_none():
    assert analyze_tick_failure({}) is None
