"""Tests for capability-scout write_guard (source-mode EACCES preflight)."""
from pathlib import Path

from agentit.write_guard import (
    ensure_writable,
    filter_stale_permission_tick_failures,
    filter_writable,
    is_writable,
    write_diff_files,
)


def test_is_writable_true_for_writable_directory(tmp_path):
    assert is_writable(tmp_path) is True
    assert is_writable(tmp_path / "new_file.py") is True


def test_is_writable_false_when_parent_missing_and_uncreatable(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "child.py"

    def _deny_access(path, mode):
        return False

    monkeypatch.setattr("agentit.write_guard.os.access", _deny_access)
    # Parent does not exist; nearest ancestor tmp_path fails access → False.
    assert is_writable(missing) is False


def test_is_writable_false_for_readonly_file(tmp_path):
    target = tmp_path / "ro.py"
    target.write_text("x\n", encoding="utf-8")
    target.chmod(0o444)
    try:
        assert is_writable(target) is False
    finally:
        target.chmod(0o644)


def test_filter_writable_keeps_only_writable(tmp_path, caplog):
    good = tmp_path / "good.py"
    bad_parent = tmp_path / "locked"
    bad_parent.mkdir()
    bad_parent.chmod(0o555)
    bad = bad_parent / "bad.py"
    try:
        import logging

        with caplog.at_level(logging.WARNING):
            kept = filter_writable([good, bad])
        assert kept == [good]
        assert "bad.py" in caplog.text or "locked" in caplog.text
    finally:
        bad_parent.chmod(0o755)


def test_ensure_writable_and_write_diff_files(tmp_path):
    ok, detail = ensure_writable(tmp_path / "tests" / "t.py")
    assert ok is True
    assert detail == ""
    wrote, err = write_diff_files(
        tmp_path,
        {"tests/test_example.py": "def test_ok():\n    assert True\n"},
    )
    assert wrote is True
    assert err == ""
    assert (tmp_path / "tests" / "test_example.py").read_text(encoding="utf-8").startswith("def test_ok")


def test_filter_stale_permission_tick_failures_drops_remediated(tmp_path):
    target = tmp_path / "tests" / "test_stack_signature_detector.py"
    target.parent.mkdir(parents=True)
    # Parent is writable → stale EACCES for a not-yet-existing file is remediated.
    events = [
        {
            "summary": (
                "capability-scout tick failed: [Errno 13] Permission denied: "
                f"'{target}'"
            ),
        },
        {"summary": "capability-scout tick failed: something else"},
    ]
    kept = filter_stale_permission_tick_failures(events, tmp_path)
    assert len(kept) == 1
    assert "something else" in kept[0]["summary"]
