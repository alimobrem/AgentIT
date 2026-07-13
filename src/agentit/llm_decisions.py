"""LLM decision audit — merges every real LLM *decision* point (not just LLM content
generation) into one queryable, attributed view.

"Decision" here means the LLM's output directly gates an outcome (approve/reject/
auto-apply/gate) — not just "the LLM generated some text". Two decision points
currently produce durable records that this module can query:

  - fix-review (`LLMClient.review_fix`, invoked from `cli.py`'s `self-fix` command's
    Step 3 first-approver gate): approves or rejects a generated fix. Persisted to the
    `skill_effectiveness` table via `record_skill_outcome()`, attributed by skill name
    (real per-skill attribution — this is the best-attributed decision point today).

  - auto-mode classify (`LLMClient.classify_action`, invoked from
    `AutoMode.should_auto_apply`/`execute`): classifies a bundle of manifests as safe
    or destructive to decide auto-apply vs. gate. Persisted to the `events` table
    (action='decision'). Attribution is the caller-supplied `agent_name` when known
    (e.g. the dispatcher's `result["agent"]`), otherwise the generic "auto-mode"
    component name — most auto-mode decisions today fall into the latter bucket
    because most callers (onboarding, self-fix) apply a whole bundle of manifests
    spanning many agents at once, not a single agent/skill's output.

A third real decision point, `classify_secret` (the security analyzer's LLM-based
false-positive filter on potential hardcoded secrets), is deliberately NOT included
here: nothing persists its verdict today — the confidence/reason is used inline to
decide whether to keep or drop a finding and then discarded. That's a real gap, not
a bug in this module; it's called out in the "LLM Decisions" page and README instead
of being silently omitted.
"""
from __future__ import annotations

DECISION_TYPE_FIX_REVIEW = "fix-review"
DECISION_TYPE_AUTO_MODE = "auto-mode-classify"

_AUTO_MODE_FALLBACK_AGENT = "auto-mode"


def _fix_review_decisions(store, limit: int) -> list[dict]:
    """`skill_effectiveness` rows → decisions attributed by real skill name."""
    rows = store.get_recent_skill_activity(limit=limit)
    return [
        {
            "timestamp": r["created_at"],
            "decision_type": DECISION_TYPE_FIX_REVIEW,
            "attribution": r["skill_name"],
            "attribution_kind": "skill",
            "target_app": r["app_name"],
            "outcome": r["outcome"],
            "reason": r.get("reason") or "",
        }
        for r in rows
    ]


def _parse_auto_mode_summary(summary: str) -> tuple[str, str]:
    """Split an auto-mode decision event's summary into (outcome, reason).

    Summaries are logged as `f"{'AUTO-APPLY' if can_apply else 'GATE'}: {reason}"`
    by `AutoMode.execute()` — see automode.py.
    """
    if ": " in summary:
        prefix, reason = summary.split(": ", 1)
    else:
        prefix, reason = summary, ""
    outcome = "auto-applied" if prefix.strip() == "AUTO-APPLY" else "gated"
    return outcome, reason


def _auto_mode_decisions(store, limit: int) -> list[dict]:
    """`events` rows (action='decision') → decisions attributed by `agent_id`.

    `agent_id` is the real originating agent/skill when the caller passed one
    through to `AutoMode.execute(agent_name=...)`; otherwise it's the generic
    "auto-mode" component name (see module docstring).
    """
    rows = store.list_events_by_action("decision", limit=limit)
    decisions = []
    for r in rows:
        outcome, reason = _parse_auto_mode_summary(r["summary"])
        agent_id = r["agent_id"]
        decisions.append({
            "timestamp": r["timestamp"],
            "decision_type": DECISION_TYPE_AUTO_MODE,
            "attribution": agent_id,
            "attribution_kind": "component" if agent_id == _AUTO_MODE_FALLBACK_AGENT else "agent",
            "target_app": r.get("target_app") or "",
            "outcome": outcome,
            "reason": reason,
        })
    return decisions


def list_llm_decisions(
    store,
    limit: int = 200,
    decision_type: str = "",
    attribution: str = "",
) -> list[dict]:
    """All real LLM decisions, newest first, optionally filtered.

    Each decision dict has: timestamp, decision_type, attribution,
    attribution_kind ("skill" | "agent" | "component"), target_app, outcome, reason.
    """
    decisions = _fix_review_decisions(store, limit) + _auto_mode_decisions(store, limit)
    if decision_type:
        decisions = [d for d in decisions if d["decision_type"] == decision_type]
    if attribution:
        decisions = [d for d in decisions if d["attribution"] == attribution]
    decisions.sort(key=lambda d: d["timestamp"], reverse=True)
    return decisions[:limit]


def summarize_by_attribution(decisions: list[dict]) -> list[dict]:
    """Group decisions by (decision_type, attribution) with outcome counts.

    Answers "how is agent/skill X actually performing" — how often the LLM
    approves vs. rejects/gates its output — the same shape as the Capabilities
    page's per-skill effectiveness column, but computed from the merged
    decision view so it sits alongside the LLM's actual reasoning text.
    """
    grouped: dict[tuple[str, str], dict] = {}
    for d in decisions:
        key = (d["decision_type"], d["attribution"])
        group = grouped.setdefault(key, {
            "decision_type": d["decision_type"],
            "attribution": d["attribution"],
            "attribution_kind": d["attribution_kind"],
            "total": 0,
            "outcomes": {},
        })
        group["total"] += 1
        group["outcomes"][d["outcome"]] = group["outcomes"].get(d["outcome"], 0) + 1

    summaries = list(grouped.values())
    summaries.sort(key=lambda g: g["total"], reverse=True)
    return summaries
