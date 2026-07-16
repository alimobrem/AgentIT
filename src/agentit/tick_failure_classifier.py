"""Classify tick-failure events and emit remediation hints."""
import re

_PATTERNS = [
    (r"\[Errno 13\] Permission denied: '([^']+)'", "permission_denied",
     "Check file ownership and permissions; run: chmod 644 {path} or chown <user> {path}"),
    (r"\[Errno 2\] No such file[^:]*: '([^']+)'", "file_not_found",
     "Ensure the file exists at {path} before running the agent."),
]


def classify(event: dict) -> dict:
    """Return error_class, affected_path, remediation_hint for a tick-failure event."""
    summary = event.get("summary") or ""
    for pattern, error_class, hint_template in _PATTERNS:
        m = re.search(pattern, summary)
        if m:
            path = m.group(1)
            return {"error_class": error_class, "affected_path": path,
                    "remediation_hint": hint_template.format(path=path)}
    return {"error_class": "unknown", "affected_path": None, "remediation_hint": None}
