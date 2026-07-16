"""LLM decision audit — merges every real LLM *decision* point (not just LLM content
generation) into one queryable, attributed view.

"Decision" here means the LLM's output directly gates an outcome (approve/reject/
auto-apply/gate) — not just "the LLM generated some text". Four decision points
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

  - secret-classify (`LLMClient.classify_secret`, invoked from
    `analyzers.security.SecurityAnalyzer._check_secrets`): decides per regex match
    whether a potential hardcoded secret is real (`kept` — stays a critical finding)
    or a false positive (`dropped` — confidence > 0.7 that it isn't a secret, so the
    finding is discarded before it ever reaches the report). Persisted to the `events`
    table (action='secret-classify') via `build_secret_classify_events()`, attributed
    generically to the `security-analyzer` component (there's no per-skill target —
    every classification happens inside one analyzer, not a generated skill), with
    `target_app` carrying the real repo being scanned.

  - capability-proposal (`LLMClient.propose_capability_improvement`, invoked from
    the `capability-scout` watcher — see docs/self-improvement-for-agentit.md),
    proposes small, evidence-grounded changes to AgentIT's own codebase. Persisted to
    the `events` table (action='capability-run') every cycle, whether or not a
    proposal was actually made — `_capability_proposal_decisions()` maps each cycle to
    a decision, attributed generically to the `capability-scout` component (there's no
    per-app or per-skill target — every proposal targets AgentIT's own repo).
"""
from __future__ import annotations

import asyncio
import json

DECISION_TYPE_FIX_REVIEW = "fix-review"
DECISION_TYPE_AUTO_MODE = "auto-mode-classify"
DECISION_TYPE_SECRET_CLASSIFY = "secret-classify"  # == analyzers.security.SECRET_CLASSIFY_ACTION
DECISION_TYPE_CAPABILITY_PROPOSAL = "capability-proposal"

_AUTO_MODE_FALLBACK_AGENT = "auto-mode"
_SECRET_CLASSIFY_ATTRIBUTION = "security-analyzer"
_CAPABILITY_SCOUT_ATTRIBUTION = "capability-scout"


def _bridge(result, loop):
    """Run a store call's result to completion, sync or async.

    This module is called from a worker thread via ``asyncio.to_thread``
    (see ``routes/insights.py``), so a Postgres-backed store's coroutine
    methods can't be `await`ed directly here -- they're scheduled back onto
    the event loop that constructed the store via
    ``asyncio.run_coroutine_threadsafe``, the same bridge
    ``EventConsumer._persist_dead_letter`` established in commit 7533309
    (an ``asyncpg`` pool is bound to its creating loop and can't be driven
    from a different thread's loop). A no-op passthrough against the
    sqlite backend, whose store methods return already-computed values.
    """
    if not asyncio.iscoroutine(result):
        return result
    return asyncio.run_coroutine_threadsafe(result, loop).result(timeout=30)


def _fix_review_decisions(store, limit: int, loop=None) -> list[dict]:
    """`skill_effectiveness` rows → decisions attributed by real skill name."""
    rows = _bridge(store.get_recent_skill_activity(limit=limit), loop)
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


def _auto_mode_decisions(store, limit: int, loop=None) -> list[dict]:
    """`events` rows (action='decision') → decisions attributed by `agent_id`.

    `agent_id` is the real originating agent/skill when the caller passed one
    through to `AutoMode.execute(agent_name=...)`; otherwise it's the generic
    "auto-mode" component name (see module docstring).
    """
    rows = _bridge(store.list_events_by_action("decision", limit=limit), loop)
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


def build_secret_classify_events(decisions: list[dict], target_app: str) -> list[dict]:
    """Turn `SecurityAnalyzer`'s raw per-match `classify_secret` verdicts into
    `store.log_event()` call kwargs, ready to persist once a caller has run an
    assessment (see `runner.run_assessment`'s `secret_decisions_out` param).

    Mirrors `AutoMode.execute()`'s "{OUTCOME}: {reason}" summary convention
    exactly so `_secret_classify_decisions()` below can parse it the same way
    `_parse_auto_mode_summary()` does.
    """
    from agentit.analyzers.security import SECRET_CLASSIFY_ACTION

    events = []
    for d in decisions:
        outcome = "KEPT" if d["kept"] else "DROPPED"
        events.append({
            "agent_id": _SECRET_CLASSIFY_ATTRIBUTION,
            "action": SECRET_CLASSIFY_ACTION,
            "target_app": target_app,
            "severity": "info",
            "summary": f"{outcome}: {d['reason']}",
            "details": {
                "file_path": d["file_path"],
                "secret_type": d["secret_type"],
                "is_secret": d["is_secret"],
                "confidence": d["confidence"],
            },
        })
    return events


def _parse_secret_classify_summary(summary: str) -> tuple[str, str]:
    """Split a secret-classify decision event's summary into (outcome, reason).

    Summaries are logged as `f"{'KEPT' if kept else 'DROPPED'}: {reason}"` by
    `build_secret_classify_events()` above.
    """
    if ": " in summary:
        prefix, reason = summary.split(": ", 1)
    else:
        prefix, reason = summary, ""
    outcome = "kept" if prefix.strip() == "KEPT" else "dropped"
    return outcome, reason


def _secret_classify_decisions(store, limit: int, loop=None) -> list[dict]:
    """`events` rows (action='secret-classify') → decisions attributed to the
    generic `security-analyzer` component -- every real `classify_secret` LLM
    call becomes a decision, whether the match was kept as a finding or
    dropped as a false positive (see module docstring)."""
    from agentit.analyzers.security import SECRET_CLASSIFY_ACTION

    rows = _bridge(store.list_events_by_action(SECRET_CLASSIFY_ACTION, limit=limit), loop)
    decisions = []
    for r in rows:
        outcome, reason = _parse_secret_classify_summary(r["summary"])
        decisions.append({
            "timestamp": r["timestamp"],
            "decision_type": DECISION_TYPE_SECRET_CLASSIFY,
            "attribution": _SECRET_CLASSIFY_ATTRIBUTION,
            "attribution_kind": "component",
            "target_app": r.get("target_app") or "",
            "outcome": outcome,
            "reason": reason,
        })
    return decisions


def _humanize_capability_evidence(text: str) -> str:
    """Guard against a raw JSON dump ever surfacing as "reasoning" text.

    `details["evidence"]` is normally the LLM's own free-text citation
    (e.g. "README.md:42 — Documented future idea"), but it's still LLM
    output -- nothing prevents it (or an older/legacy event's differently-
    shaped `details_json`) from being (or containing) a serialized dict
    like the real signal rows `gather_evidence()` feeds the LLM (e.g. one
    of `store.get_agent_stats()`'s own per-agent dicts: `{"agent":
    "remediation-loop", "total_events": 36, ...}`), confirmed live on the
    Decisions page -- every other decision type here (auto-mode-classify,
    secret-classify) always renders a plain sentence. If `text` parses as
    a JSON object/array, reformat it as "key: value" pairs (recursing into
    list items) instead of ever showing raw braces/quotes; otherwise it's
    already prose and is returned unchanged.
    """
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return text or ""
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return text or ""
    if isinstance(parsed, dict):
        return ", ".join(f"{k}: {v}" for k, v in parsed.items())
    if isinstance(parsed, list):
        return "; ".join(
            _humanize_capability_evidence(json.dumps(item)) if isinstance(item, (dict, list)) else str(item)
            for item in parsed
        )
    return str(parsed)


def _capability_proposal_decisions(store, limit: int, loop=None) -> list[dict]:
    """`events` rows (action='capability-run') → decisions attributed to the
    `capability-scout` component -- every cycle becomes a decision, whether
    or not it actually produced a proposal, mirroring `_auto_mode_decisions()`'s
    "generic component attribution" bucket (there's no per-app/per-skill
    target here; every proposal targets AgentIT's own repo, a constant, not
    derived per-row)."""
    from agentit.capability_scout import CAPABILITY_RUN_ACTION

    rows = _bridge(store.list_events_by_action(CAPABILITY_RUN_ACTION, limit=limit), loop)
    decisions = []
    for r in rows:
        try:
            details = json.loads(r.get("details_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        if details.get("pr_url"):
            outcome = "proposed"
        elif r["severity"] == "error":
            outcome = "error"
        elif any(not g.get("passed", True) for g in (details.get("gate_results") or [])):
            outcome = "gate-blocked"
        else:
            outcome = "no-signal"
        decisions.append({
            "timestamp": r["timestamp"],
            "decision_type": DECISION_TYPE_CAPABILITY_PROPOSAL,
            "attribution": _CAPABILITY_SCOUT_ATTRIBUTION,
            "attribution_kind": "component",
            "target_app": "agentit",
            "outcome": outcome,
            "reason": _humanize_capability_evidence(details.get("evidence") or r.get("summary", "")),
        })
    return decisions


def list_llm_decisions(
    store,
    limit: int = 200,
    decision_type: str = "",
    attribution: str = "",
    loop=None,
) -> list[dict]:
    """All real LLM decisions, newest first, optionally filtered.

    Each decision dict has: timestamp, decision_type, attribution,
    attribution_kind ("skill" | "agent" | "component"), target_app, outcome, reason.

    ``loop`` should be the event loop that constructed ``store`` (see
    ``routes/insights.py``'s call site) -- only needed when ``store`` is the
    Postgres-backed async store; ignored for the sqlite backend.
    """
    decisions = (
        _fix_review_decisions(store, limit, loop)
        + _auto_mode_decisions(store, limit, loop)
        + _secret_classify_decisions(store, limit, loop)
        + _capability_proposal_decisions(store, limit, loop)
    )
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
