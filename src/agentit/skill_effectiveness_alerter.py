"""Emit structured warnings for zero-approval-rate skills."""
import logging

logger = logging.getLogger(__name__)


def check_skill_effectiveness(stats: list, threshold: int = 3) -> list:
    """Return slugs of skills with approval_rate==0.0 and total>=threshold."""
    alerted = []
    for record in stats:
        skill = record.get("skill", "unknown")
        rate = record.get("approval_rate", 1.0)
        total = record.get("total", 0)
        if total >= threshold and rate == 0.0:
            logger.warning(
                "Zero approval rate detected: skill=%s total=%d approval_rate=%s",
                skill, total, rate,
            )
            alerted.append(skill)
    return alerted
