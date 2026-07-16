"""Tests for tick_failure_classifier."""
from src.agentit.tick_failure_classifier import classify

EVIDENCE_EVENT = {
    "id": "da3eacbb80f2434ba5f5039da5cd9c72",
    "action": "tick-failed",
    "summary": (
        "capability-scout tick failed: [Errno 13] Permission denied: "
        "'/opt/app-root/src/tests/test_stack_signature_detector.py'"
    ),
}


def test_permission_denied_from_evidence():
    r = classify(EVIDENCE_EVENT)
    assert r["error_class"] == "permission_denied"
    assert r["affected_path"] == "/opt/app-root/src/tests/test_stack_signature_detector.py"
    assert "chmod" in r["remediation_hint"]


def test_no_match_returns_unknown():
    r = classify({"summary": "capability-scout tick failed: some other error"})
    assert r["error_class"] == "unknown"
    assert r["remediation_hint"] is None


def test_file_not_found():
    r = classify({"summary": "tick failed: [Errno 2] No such file or directory: '/tmp/missing.py'"})
    assert r["error_class"] == "file_not_found"
    assert r["affected_path"] == "/tmp/missing.py"


def test_missing_summary_safe():
    assert classify({})["error_class"] == "unknown"
    assert classify({"summary": None})["error_class"] == "unknown"
