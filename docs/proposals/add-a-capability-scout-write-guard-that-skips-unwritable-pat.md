# Proposal: Add a capability-scout write-guard that skips unwritable paths before attempting file creation

> Proposed by AgentIT's capability-scout — see docs/self-improvement-for-agentit.md

**Risk:** low

## Gap

The tick_failures evidence shows capability-scout repeatedly failing with '[Errno 13] Permission denied' when trying to write test files. The existing tick_failure_classifier (merged PR #23) detects and hints about this error after the fact, but capability-scout itself has no pre-flight write-guard to skip unwritable paths before attempting creation, causing avoidable tick failures.

## Evidence

tick_failures: {agent_id: 'capability-scout', summary: 'capability-scout tick failed: [Errno 13] Permission denied: /opt/app-root/src/tests/test_stack_signature_detector.py'}. tick_failure_classifier already merged (PR#23) handles post-hoc hints but no pre-flight guard exists in capability-scout itself.

## Suggested target files

- `src/agentit/write_guard.py`
- `tests/test_write_guard.py`

## Suggested change

Add src/agentit/write_guard.py: a small module exposing `is_writable(path: str) -> bool` (checks os.access on the file if it exists, or the parent directory otherwise) and `filter_writable(paths: list[str]) -> list[str]` that returns only writable paths with a structured log warning for each skipped path. Capability-scout can import and call filter_writable before attempting file writes. tests/test_write_guard.py asserts: writable dir returns True, non-existent parent returns False, existing unwritable file returns False, filter_writable drops unwritable paths and keeps writable ones.

## Test plan

tests/test_write_guard.py uses tmp_path and monkeypatch to assert: (1) is_writable returns True for a writable directory, (2) is_writable returns False for a path whose parent does not exist, (3) is_writable returns False for a file with mode 0o444 (read-only), (4) filter_writable([writable, unwritable]) returns only the writable path, (5) filter_writable emits a log warning containing the skipped path name.
