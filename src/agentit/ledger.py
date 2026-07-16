"""The Ledger — one queryable stream over events, gates, deliveries, and
fix-review decisions. See docs/ledger-design-spec.md for the full design;
this module implements that spec's Phase 1 backing query (plus the §2
noise-at-scale shaping and the §4 rewind chain query) -- read-only, no
changes to any existing write path.

``get_ledger_cards()`` unions the four real tables into the card shapes
listed in the spec's §1 table (card types A-P) and returns them newest
first. Every mapping below is keyed off a real, already-produced
``action``/``gate_type``/``mechanism`` value -- nothing here invents new
telemetry; Phase 0 (this module's sibling changes in ``slo_tracker.py``/
``drift_detector.py``) fills the two real gaps the spec identified so
that cards J and K have something to render.

``group_cards_by_app()``, ``recent_watcher_failures()``, and
``get_chain_cards()`` back the fleet-wide grouped view, the "Needs You"
watcher-health signal, and the rewind scrubber (§2/§4) respectively --
all pure/read-only reshaping of what ``get_ledger_cards()`` (or, for the
chain query, the same ``list_events_by_correlation_id`` the Events page's
existing "Chain" link already runs) already returns.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentit.capability_scout import CAPABILITY_RUN_ACTION
from agentit.learning_agent import LEARNING_RUN_ACTION

# §2 rule 4: collapse consecutive tick-complete events from the same watcher;
# a tick-failed always stays its own card and breaks the run.
_TICK_ACTIONS = ("tick-complete", "tick-failed")

# Every event `action` this module knows how to turn into a card, mapped to
# its card type letter from docs/ledger-design-spec.md §1. An action not in
# this table produces no card (rather than an unlabeled generic one) --
# extend this table, don't add a fallback bucket, so every card type stays
# traceable to a real spec entry.
_EVENT_ACTION_TO_CARD_TYPE: dict[str, str] = {
    "assessment-complete": "A",
    "reassessment-complete": "A",
    "fix-generated": "B",
    "onboarding-complete": "B",
    "decision": "C",
    "tick-complete": "H",
    "tick-failed": "H",
    "critical-findings-detected": "I",
    "remediation-failed": "I",
    "rollback-recommended": "J",
    "drift-detected": "K",
    "drift-auto-synced": "K",
    "drift-auto-sync-failed": "K",
    "api-removed": "L",
    "skill-deprecated": "L",
    LEARNING_RUN_ACTION: "M",
    "skill-added": "N",
    "skill-removed": "N",
    "check-added": "N",
    "check-removed": "N",
    "skill-activated": "N",
    CAPABILITY_RUN_ACTION: "O",
    "auto-mode-toggled": "P",
    "auto-mode-allowlist-added": "P",
    "auto-mode-allowlist-removed": "P",
}

_GATE_STATUS_TO_CARD_TYPE = {
    "pending": "D",
    "approved": "E",
    "rejected": "E",
    "expired": "E",
    "cancelled": "E",
}


def _tick_run_summary(agent_id: str, run: list[dict]) -> dict:
    """One or more consecutive ``tick-complete`` events -> a single event
    dict. A run of exactly one is returned unchanged (no point summarizing
    a single tick); a longer run becomes one synthetic low-salience row
    ("watcher: N clean ticks since <first tick's time>"), positioned at the
    *last* tick's timestamp so it sorts where the most recent confirmation
    actually happened."""
    if len(run) == 1:
        return run[0]
    first, last = run[0], run[-1]
    collapsed = dict(last)
    collapsed["summary"] = f"{agent_id}: {len(run)} clean ticks since {first['timestamp'][:16]}"
    collapsed["correlation_id"] = None  # a collapsed run has no single chain to replay
    return collapsed


def _collapse_tick_events(tick_events: list[dict]) -> list[dict]:
    """Per docs/ledger-design-spec.md §2 rule 4. Only ever called on the
    tick-complete/tick-failed subset of events -- every other action passes
    through ``get_ledger_cards`` untouched by this function."""
    by_agent: dict[str, list[dict]] = {}
    for e in tick_events:
        by_agent.setdefault(e["agent_id"], []).append(e)

    collapsed: list[dict] = []
    for agent_id, agent_events in by_agent.items():
        ordered = sorted(agent_events, key=lambda e: e["timestamp"])
        run: list[dict] = []
        for e in ordered:
            if e["action"] == "tick-complete":
                run.append(e)
                continue
            # tick-failed breaks the run: flush what came before, then the
            # failure itself renders as its own full-salience card.
            if run:
                collapsed.append(_tick_run_summary(agent_id, run))
                run = []
            collapsed.append(e)
        if run:
            collapsed.append(_tick_run_summary(agent_id, run))
    return collapsed


def _annotate_chain_counts(event_cards: list[dict]) -> None:
    """Mutates each event-sourced card in place with ``chain_count`` --
    how many cards in *this same fetch* share its ``correlation_id`` --
    so the "Part of a chain (N events)" affordance never issues an extra
    query just to know N. Gates/deliveries/fix-reviews have no
    ``correlation_id`` of their own (per spec §1, they're joined to a
    chain via ``assessment_id`` instead), so this only ever touches cards
    sourced from ``events``."""
    counts: dict[str, int] = {}
    for c in event_cards:
        cid = c.get("correlation_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    for c in event_cards:
        cid = c.get("correlation_id")
        c["chain_count"] = counts.get(cid, 0) if cid else 0


def group_cards_by_app(cards: list[dict]) -> dict[str, list[dict]]:
    """Group an already-fetched, newest-first card list by ``target_app``,
    preserving each app's relative newest-first order. Backs the fleet-wide
    Ledger's grouped-by-app view (§2 rule 2): ``bucket[0]`` is always that
    app's single most-recent/most-severe card to summarize inline before
    it's expanded."""
    by_app: dict[str, list[dict]] = {}
    for c in cards:
        app_name = c.get("target_app")
        if app_name:
            by_app.setdefault(app_name, []).append(c)
    return by_app


def recent_watcher_failures(cards: list[dict], *, hours: int = 4) -> list[dict]:
    """§2 rule 3's 4th "Needs You" signal: a watcher's last tick failed
    within the configured interval -- the same real ``tick-failed`` event
    every watcher already logs (``watchers/__init__.py::record_tick``), the
    same signal ``AgentITWatcherStale`` alerts on via Prometheus. Fleet-wide
    (ticks aren't scoped to one app), so this is surfaced as its own banner
    rather than forced into a per-app row."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return [
        c for c in cards
        if c.get("card_type") == "H" and c.get("title") == "tick-failed" and c["timestamp"] >= cutoff
    ]


def _event_card(event: dict) -> dict | None:
    card_type = _EVENT_ACTION_TO_CARD_TYPE.get(event["action"])
    if card_type is None:
        return None
    return {
        "card_type": card_type,
        "timestamp": event["timestamp"],
        "target_app": event.get("target_app"),
        "severity": event.get("severity", "info"),
        "title": event["action"],
        "summary": event.get("summary", ""),
        "agent_id": event.get("agent_id"),
        "correlation_id": event.get("correlation_id"),
        "source": "events",
        "raw": event,
    }


def _gate_card(gate: dict, *, known_app_name: str | None = None) -> dict:
    """``gates`` has no ``app_name``/``target_app`` column of its own --
    ``list_all_gates()`` supplies it via a join (``gate.get("app_name")``),
    but ``list_gates_for_assessment()`` doesn't, so the per-app call site
    passes ``known_app_name`` (the caller already knows which app it asked
    for) instead."""
    card_type = _GATE_STATUS_TO_CARD_TYPE.get(gate["status"], "E")
    return {
        "card_type": card_type,
        "timestamp": gate.get("resolved_at") or gate["created_at"],
        "target_app": known_app_name or gate.get("app_name"),
        "severity": "warning" if gate["status"] == "pending" else "info",
        "title": gate["gate_type"],
        "summary": gate.get("summary", ""),
        "assessment_id": gate.get("assessment_id"),
        "gate_status": gate["status"],
        "source": "gates",
        "raw": gate,
    }


def _delivery_card(delivery: dict) -> dict:
    return {
        "card_type": "F",
        "timestamp": delivery["created_at"],
        "target_app": delivery.get("app_name"),
        "severity": "info",
        "title": f"delivery-{delivery['mechanism']}",
        "summary": (
            f"Delivered via {delivery['mechanism']} "
            f"(verification: {delivery.get('verification', 'unknown')})"
        ),
        "assessment_id": delivery.get("assessment_id"),
        "mechanism": delivery["mechanism"],
        "source": "deliveries",
        "raw": delivery,
    }


def _fix_review_card(activity: dict) -> dict:
    """`skill_effectiveness` rows -> card type G, mirroring
    llm_decisions.py's `_fix_review_decisions()` shape (same source, same
    attribution), but returned as a Ledger card rather than a Decisions-page
    row."""
    return {
        "card_type": "G",
        "timestamp": activity["created_at"],
        "target_app": activity.get("app_name"),
        "severity": "info",
        "title": f"fix-review-{activity['outcome']}",
        "summary": activity.get("reason") or f"Fix {activity['outcome']} for skill {activity['skill_name']}",
        "attribution": activity["skill_name"],
        "source": "llm_decisions",
        "raw": activity,
    }


async def get_ledger_cards(
    store: object,
    *,
    target_app: str | None = None,
    assessment_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Union events + gates + deliveries + fix-review decisions into Ledger
    cards, newest first.

    Scoping: pass ``target_app`` for a per-app stream (events/deliveries/
    fix-reviews are keyed by app name; gates are keyed by ``assessment_id``,
    so they're fetched via ``list_gates_for_assessment`` when
    ``assessment_id`` is also given -- see docs/architecture.md's
    "Data model: assessments vs. apps" for why gates need the extra hop).
    Pass neither for the fleet-wide view.
    """
    cards: list[dict] = []

    if target_app:
        events = await store.list_events(limit=limit, target_app=target_app)
    else:
        events = await store.list_events(limit=limit)
    tick_events = [e for e in events if e["action"] in _TICK_ACTIONS]
    other_events = [e for e in events if e["action"] not in _TICK_ACTIONS]
    events = other_events + _collapse_tick_events(tick_events)
    event_cards = [c for e in events if (c := _event_card(e)) is not None]
    _annotate_chain_counts(event_cards)
    cards.extend(event_cards)

    if assessment_id:
        gates = await store.list_gates_for_assessment(assessment_id)
        cards.extend(_gate_card(g, known_app_name=target_app) for g in gates[:limit])
    else:
        gates = await store.list_all_gates()
        cards.extend(_gate_card(g) for g in gates[:limit])

    if assessment_id:
        deliveries = await store.list_deliveries(assessment_id)
    else:
        deliveries = await store.list_all_deliveries(limit=limit)
    cards.extend(_delivery_card(d) for d in deliveries)

    fix_reviews = await store.get_recent_skill_activity(limit=limit)
    if target_app:
        fix_reviews = [a for a in fix_reviews if a.get("app_name") == target_app]
    cards.extend(_fix_review_card(a) for a in fix_reviews)

    cards.sort(key=lambda c: c["timestamp"], reverse=True)
    return cards[:limit]


async def get_chain_cards(store: object, correlation_id: str) -> list[dict]:
    """Backs the rewind scrubber (docs/ledger-design-spec.md §4) -- the
    non-speculative "replay history" half only, never the ruled-out
    forward-looking half. Reuses, verbatim, the same data this system
    already computes for the exact same purpose:

    - ``events`` rows ``WHERE correlation_id = X`` -- the same query the
      Events page's existing "Chain" link already runs
      (``list_events_by_correlation_id``, oldest first here so the
      scrubber plays back in the order it actually happened).
    - ``gates`` rows and the ``deliveries`` row(s) for the assessment_id
      that correlation_id resolves to, for every app touched by the chain.
    - ``skill_effectiveness`` (fix-review) rows for those same apps,
      restricted to the chain's own time span.

    Returns cards oldest-first, each still carrying its own ``gate_status``/
    ``mechanism``/etc. Callers render these read-only -- no action buttons --
    since a resolved gate has nothing left to approve and a delivery that
    already happened can't be re-decided.
    """
    events = await store.list_events_by_correlation_id(correlation_id, limit=500)
    cards = [c for e in events if (c := _event_card(e)) is not None]
    if not cards:
        return []

    target_apps = {c["target_app"] for c in cards if c.get("target_app")}
    fleet = await store.get_fleet_data()
    assessment_id_by_app = {a["repo_name"]: a["id"] for a in fleet if a.get("repo_name") in target_apps}

    start, end = cards[0]["timestamp"], cards[-1]["timestamp"]
    for app_name, aid in assessment_id_by_app.items():
        gates = await store.list_gates_for_assessment(aid)
        cards.extend(_gate_card(g, known_app_name=app_name) for g in gates)
        deliveries = await store.list_deliveries(aid)
        cards.extend(_delivery_card(d) for d in deliveries)

    if target_apps:
        fix_reviews = await store.get_recent_skill_activity(limit=500)
        cards.extend(
            _fix_review_card(a) for a in fix_reviews
            if a.get("app_name") in target_apps and start <= a["created_at"] <= end
        )

    cards.sort(key=lambda c: c["timestamp"])
    return cards
