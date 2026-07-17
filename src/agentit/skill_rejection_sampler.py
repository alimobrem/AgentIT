"""Records structured rejection context for low-effectiveness skills."""
import json, logging, os
from collections import Counter
from datetime import datetime, timezone

DEFAULT_LOG = "skill_rejections.jsonl"
log = logging.getLogger(__name__)

def record_rejection(skill_name, context, reason="unspecified", path=DEFAULT_LOG, approval_rate=None, threshold=0.20):
    """Append one JSON rejection record; warn if approval_rate is below threshold."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "skill": skill_name, "reason": reason, "context": context}
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    if approval_rate is not None and approval_rate < threshold:
        log.warning("low-effectiveness skill '%s': approval_rate=%.2f < threshold=%.2f", skill_name, approval_rate, threshold)

def summarize_rejections(skill_name, path=DEFAULT_LOG):
    """Return Counter of rejection reasons for skill_name."""
    counts = Counter()
    if not os.path.exists(path):
        return counts
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("skill") == skill_name:
                    counts[r.get("reason", "unspecified")] += 1
            except json.JSONDecodeError:
                pass
    return counts
