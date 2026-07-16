"""Records structured rejection reasons for low-effectiveness skills."""
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List
import structlog

log = structlog.get_logger()
DEFAULT_CAP = 200
REJECTION_RATE_THRESHOLD = 0.8


class RejectionSampler:
    """Collects rejection samples and warns when a skill's rejection rate is high."""

    def __init__(self, cap: int = DEFAULT_CAP) -> None:
        self._samples: deque = deque(maxlen=cap)
        self._counts: Dict[str, Dict[str, int]] = {}

    def record(self, skill: str, reason: str, metadata: Dict[str, Any]) -> None:
        """Record a rejection event for a skill."""
        entry = {
            "skill": skill,
            "reason": reason,
            "metadata": metadata,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._samples.append(entry)
        counts = self._counts.setdefault(skill, {"approved": 0, "rejected": 0})
        counts["rejected"] += 1
        total = counts["approved"] + counts["rejected"]
        rate = counts["rejected"] / total if total else 0.0
        if rate >= REJECTION_RATE_THRESHOLD:
            log.warning(
                "high_rejection_rate",
                skill=skill,
                rejection_rate=rate,
                total=total,
            )

    def approve(self, skill: str) -> None:
        """Record an approval event (lowers rejection rate)."""
        counts = self._counts.setdefault(skill, {"approved": 0, "rejected": 0})
        counts["approved"] += 1

    def get_samples(self, skill: str) -> List[Dict[str, Any]]:
        """Return all recorded samples for a given skill."""
        return [s for s in self._samples if s["skill"] == skill]
