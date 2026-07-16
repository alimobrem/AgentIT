"""Surfaces the documented 'fast-forward preview' gap as a structured warning."""
import json
import logging
import warnings

log = logging.getLogger(__name__)

GAP_INFO = {
    "gap": "predictive fast-forward preview",
    "doc": "docs/ledger-design-spec.md",
    "line_no": 193,
    "note": "Explicitly not built",
}


def check_fastforward_gap() -> dict:
    """Emit a UserWarning and log entry for the fast-forward gap; return gap dict."""
    msg = (
        "fast-forward preview feature is 'Explicitly not built' "
        f"(see {GAP_INFO['doc']} line {GAP_INFO['line_no']})"
    )
    warnings.warn(msg, UserWarning, stacklevel=2)
    log.warning(json.dumps({"event": "gap_audit", **GAP_INFO}))
    return dict(GAP_INFO)
