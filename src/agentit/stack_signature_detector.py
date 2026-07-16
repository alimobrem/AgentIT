"""Detect repeated stack patterns across onboardings."""
from datetime import datetime, timedelta, timezone
from collections import Counter


def detect_repeated_stack_patterns(assessments, threshold=3, window_days=30):
    """Return stack signatures seen >= threshold times within window_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    counts = Counter()
    for a in assessments:
        ts = a.get("timestamp")
        if ts and ts < cutoff:
            continue
        stack = a.get("stack")
        if stack:
            counts[stack] += 1
    return [s for s, c in counts.items() if c >= threshold]
