"""Detect permission-denied tick failures and emit remediation hints."""
import re

_PERM_RE = re.compile(r"Permission denied[:\s]+(?P<path>\S+)")


def analyze_tick_failure(event: dict):
    """Return a remediation hint dict or None."""
    summary = event.get("summary", "")
    if not summary:
        return None
    if "Permission denied" not in summary and "Errno 13" not in summary:
        return None
    m = _PERM_RE.search(summary)
    path = m.group("path") if m else "unknown"
    return {
        "affected_path": path,
        "hint_message": "permission denied; check file/directory ownership and mode",
        "suggested_command": f"chmod a+r {path}",
    }
