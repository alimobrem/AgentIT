"""Build per-finding capability coverage overlap maps."""
import logging

log = logging.getLogger(__name__)


def build_overlap_map(findings: list, capabilities: list) -> dict:
    """Map each finding id to capability names that cover it.

    Args:
        findings: list of dicts with at least an 'id' key.
        capabilities: list of dicts with 'name' and 'finding_ids' (list) keys.

    Returns:
        dict mapping finding id -> list of covering capability names.
    """
    if not findings:
        return {}

    result = {f["id"]: [] for f in findings}

    for cap in capabilities:
        for fid in cap.get("finding_ids", []):
            if fid in result:
                result[fid].append(cap["name"])

    for fid, caps in result.items():
        if not caps:
            log.warning("Finding %r has zero capability coverage.", fid)

    return result
