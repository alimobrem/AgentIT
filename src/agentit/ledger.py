"""The Ledger ? one queryable stream over events, gates, deliveries, and
fix-review decisions. See docs/ledger-design-spec.md for the full design;
this module implements that spec's Phase 1 backing query (plus the ?2
noise-at-scale shaping and the ?4 rewind chain query) -- read-only, no
changes to any existing write path.

``get_ledger_cards()`` unions the four real tables into the card shapes
listed in the spec's ?1 table (card types A-P) and returns them newest
first. Every mapping below is keyed off a real, already-produced
``action``/``gate_type``/``mechanism`` value -- nothing here invents new
telemetry; Phase 0 (this module's sibling changes in ``slo_tracker.py``/
``drift_detector.py``) fills the two real gaps the spec identified so
that cards J and K have something to render.

``group_cards_by_app()``, ``recent_watcher_failures()``, and
``get_chain_cards()`` back the fleet-wide grouped view, the "Needs You"
watcher-health signal, and the rewind scrubber (?2/?4) respectively --
all pure/read-only reshaping of what ``get_ledger_cards()`` (or, for the
chain query, the same ``list_events_by_correlation_id`` the Events page's
existing "Chain" link already runs) already returns.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentit.analyzers.security import SECRET_CLASSIFY_ACTION
from agentit.capability_scout import CAPABILITY_RUN_ACTION
from agentit.learning_agent import LEARNING_RUN_ACTION

# ?2 rule 4: collapse consecutive tick-complete events from the same watcher;
# a tick-failed always stays its own card and breaks the run.
_TICK_ACTIONS = ("tick-complete", "tick-failed")

# Every event `action` this module knows how to turn into a card, mapped to
# its card type letter from docs/ledger-design-spec.md ?1. An action not in
# this table produces no card (rather than an unlabeled generic one) --
# extend this table, don't add a fallback bucket, so every card type stays
# traceable to a real spec entry.
_EVENT_ACTION_TO_CARD_TYPE: dict[str, str] = {
    "assessment-complete": "A",
    "reassessment-complete": "A",
    "fix-generated": "B",
    "onboarding-complete": "B",
    "decision": "C",
    # docs/ledger-design-spec.md ?3's own claim that the Decisions page is
    # "Absorbed as card types C and G" only holds if every one of its four
    # decision types actually maps to a card -- secret-classify (the other
    # non-fix-review, non-capability-proposal decision type) was missing
    # here, so every real classify_secret verdict silently vanished from
    # the Ledger even though it's a real, already-persisted decision (see
    # `llm_decisions.DECISION_TYPE_SECRET_CLASSIFY`). Card C's shape
    # (`_event_card()`) doesn't assume auto-mode's specific summary
    # wording, so a secret-classify event renders there correctly as-is.
    SECRET_CLASSIFY_ACTION: "C",
    "tick-complete": "H",
    "tick-failed": "H",
    # Rollout-verification outcomes for the shared verify_and_close_
    # delivery() tail (delivery.py) -- the same lifecycle as the "Delivery
    # routed" card F's `deliveries` row, just its later, previously-silent
    # verification step (docs/onboarding-loop-vision-gap-analysis.md
    # Phase 0 item 3): before this, a delivery's status column changed to
    # verified/rolled_back/breach-reported with no event, so nothing new
    # ever showed up here once a delivery was confirmed healthy or found
    # to have failed.
    "delivery-verified": "F",
    "delivery-rolled-back": "F",
    "delivery-breach-reported": "F",
    # Finding-scoped re-verification (docs/onboarding-loop-vision-gap-
    # analysis.md Phase 3): a delivery's specific target finding confirmed
    # gone (or still there) on a later push-triggered re-assessment -- the
    # same "delivery outcome" bucket as the SLO-verification cards above
    # for a resolved finding, the same "needs attention" bucket "remediation-
    # failed" already occupies for a still-present one.
    "delivery-finding-resolved": "F",
    "delivery-finding-still-present": "I",
    # Phase 4's bounded auto-escalation: a fresh fix attempt re-dispatched
    # below the failure threshold reads like any other freshly-generated
    # fix; a finding escalated to a human at/above the threshold is a
    # "needs attention" signal in its own right, in addition to (not
    # instead of) the real pending gate (`ESCALATION_GATE_TYPE`) it also
    # creates -- card D for that gate.
    "finding-redispatched": "B",
    "finding-redispatch-no-fix": "I",
    "finding-escalated": "I",
    "critical-findings-detected": "I",
    "remediation-failed": "I",
    # Not a "finding" in the literal sense, but the same "something in the
    # pipeline silently failed and needs attention" bucket "remediation-
    # failed" above already occupies -- assess_submit()'s background job
    # previously only logged this to the server, invisible anywhere a
    # human would actually look (docs/onboarding-loop-vision-gap-analysis.md
    # Phase 0 item 4).
    "infra-repo-creation-failed": "I",
    # The automatic Dry Run -> Deliver chain (docs/onboarding-loop-vision-
    # gap-analysis.md Phase 3) halting at a real gate -- no infra repo
    # known, a routing error, etc. -- is exactly the same "something in the
    # pipeline needs attention" shape as the other two entries in this
    # bucket. A successful auto-chain deliberately has no event mapped
    # here: `route_and_deliver()`'s own `deliveries` row already produces
    # card F for it, and adding a second card for the same delivery would
    # be a duplicate, not new signal.
    "onboard-auto-deliver-blocked": "I",
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
    """Per docs/ledger-design-spec.md ?2 rule 4. Only ever called on the
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
    ``correlation_id`` of their own (per spec ?1, they're joined to a
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
    Ledger's grouped-by-app view (?2 rule 2): ``bucket[0]`` is always that
    app's single most-recent/most-severe card to summarize inline before
    it's expanded."""
    by_app: dict[str, list[dict]] = {}
    for c in cards:
        app_name = c.get("target_app")
        if app_name:
            by_app.setdefault(app_name, []).append(c)
    return by_app


def recent_watcher_failures(cards: list[dict], *, hours: int = 4) -> list[dict]:
    """?2 rule 3's 4th "Needs You" signal: a watcher's last tick failed
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


# Human labels for the card_type letter (docs/ledger-design-spec.md §1's
# A-P table) -- the letter itself stays the real filter value/query param
# (ledger.html's <option value="{{ letter }}">, insights.py's card_type
# query arg) so nothing about the underlying data model or route changes;
# only what's ever shown to a user is affected. Deliberately a category
# name distinct from a card's own humanized title (see
# _EVENT_ACTION_TITLES below) rather than restating it -- "Gate opened" +
# "GitOps PR pending" on the same card is two levels of real information,
# not one fact said twice.
CARD_TYPE_LABELS: dict[str, str] = {
    "A": "Assessment",
    "B": "Fix / onboard generated",
    "C": "Classifier",
    "D": "Gate opened",
    "E": "Gate resolved",
    "F": "Delivery",
    "G": "Fix review",
    "H": "Watcher tick",
    "I": "Needs attention",
    "J": "SLO breach",
    "K": "Drift",
    "L": "Self-correction",
    "M": "Learning run",
    "N": "Catalog change",
    "O": "Self-improvement",
    "P": "Setting change",
}


def humanize_card_type(card_type: str) -> str:
    """Decode a card_type letter into its real category name (see
    CARD_TYPE_LABELS above) -- never the bare letter, which means nothing
    to a user without reading this module's own source. Falls back to the
    letter itself for any value outside A-P (there shouldn't be one, but a
    lookup miss should degrade to "unlabeled", not a blank/KeyError).

    Public (not just this module's own Ledger badge use): also registered
    as the ``humanize_card_type`` Jinja filter (see portal/app.py) so
    ledger.html's "Card type" filter dropdown decodes the same table
    instead of listing bare letters A-P with no explanation.
    """
    return CARD_TYPE_LABELS.get(card_type, card_type)


# Human labels for a gate's real gate_type -- gate_card() (_macros.html)
# used to render this as `{{ gate.gate_type | upper }}`, e.g.
# "GITOPS-PR-PENDING-SHARED-NAMESPACE" or "AUTO-MODE-REVIEW", an all-caps
# hyphenated internal identifier with no spaces, on both Admin Review and
# every app's Actions tab. Deliberately not a strict dict-only lookup like
# CARD_TYPE_LABELS above: gate types have changed more than once this
# session (cluster-conflict-review and auto-mode-scope-review retired,
# cluster-admin-review retired but still rendered for in-flight rows,
# gitops-pr-pending-shared-namespace added) and the "finding-{category}"
# family covers whichever check dimensions checks/ actually has -- a
# fallback that degrades gracefully to a readable phrase (never a raw,
# unhumanized value) matters more here than an exhaustive table that goes
# stale the next time a gate type is renamed.
GATE_TYPE_LABELS: dict[str, str] = {
    "cluster-admin-review": "Cluster-admin review",
    "gitops-pr-pending": "GitOps PR pending",
    "gitops-pr-pending-shared-namespace": "GitOps PR pending (shared namespace)",
    "auto-mode-review": "Auto-mode review",
    "rollback-review": "Rollback review",
    "finding-unresolved-escalation": "Unresolved finding escalation",
}


def humanize_gate_type(gate_type: str) -> str:
    """Decode a gate's gate_type into a real, readable phrase -- never the
    raw hyphenated identifier. Checks the explicit table first; a
    "finding-{category}" gate (one per check dimension -- see
    routes/webhooks.py's per-category dispatcher) becomes "{Category}
    finding"; anything else degrades to Title Case with hyphens/
    underscores turned into spaces, so an unrecognized or future gate type
    still reads as a phrase instead of a raw identifier.

    Public: also registered as the ``humanize_gate_type`` Jinja filter
    (see portal/app.py) for _macros.html's shared ``gate_card()``.
    """
    if not gate_type:
        return gate_type
    if gate_type in GATE_TYPE_LABELS:
        return GATE_TYPE_LABELS[gate_type]
    if gate_type.startswith("finding-"):
        category = gate_type[len("finding-"):].replace("-", " ").replace("_", " ").strip()
        return f"{category.capitalize()} finding" if category else "Finding"
    return gate_type.replace("-", " ").replace("_", " ").strip().capitalize()


# Human phrases for a raw events.action value -- Capabilities' "Recent
# Catalog Changes" table (and any other page rendering a raw action
# string straight from the events table) showed e.g. "skill-added",
# "check-removed" verbatim. Not exhaustive by design (see
# humanize_gate_type()'s docstring for why a graceful fallback beats a
# table that must be kept in lockstep with every log_event() call site):
# only the handful of actions a real template renders directly today are
# listed; everything else degrades to a readable phrase instead of the
# raw hyphenated identifier.
_ACTION_LABELS: dict[str, str] = {
    "skill-added": "Skill added",
    "skill-removed": "Skill removed",
    "check-added": "Check added",
    "check-removed": "Check removed",
    "skill-activated": "Skill activated",
    "skill-deprecated": "Skill deprecated",
}


def humanize_action(action: str) -> str:
    """Decode a raw events.action value into a readable phrase. Checks the
    explicit table first, then degrades to Title Case with hyphens/
    underscores turned into spaces -- never the raw identifier.

    Public: also registered as the ``humanize_action`` Jinja filter (see
    portal/app.py).
    """
    if not action:
        return action
    if action in _ACTION_LABELS:
        return _ACTION_LABELS[action]
    return action.replace("-", " ").replace("_", " ").strip().capitalize()


_MECHANISM_SHORT_LABELS: dict[str, str] = {
    "direct-apply": "Applied directly",
    # These three all say plainly *which* repo the PR/commit targets --
    # AgentIT apps have two distinct repos in play (report.repo_url, the
    # app's own code repo; report.infra_repo_url, its GitOps repo -- see
    # delivery.py's repo_kind_for_mechanism()), and a bare "PR opened" left
    # every Ledger card and Delivery History row ambiguous about which one.
    "infra-repo-commit": "PR opened against the GitOps repo",
    "cluster-admin-review-gate": "Cluster-admin review required",
    "source-repo-pr": "Source-patch PR opened against the code repo",
    "app-repo-pr": "App-repo PR opened against the code repo",
    "none": "Nothing delivered",
}


def humanize_delivery_mechanism(mechanism: str) -> str:
    """Decode deliveries.mechanism into a short, human phrase -- never the
    raw category:mechanism routing string route_and_deliver() persists
    there. The raw value is either a single mechanism (e.g.
    "direct-apply") or, for a multi-category delivery, a comma-joined
    "category:mechanism" string (e.g. "cluster_config:direct-apply,
    cicd_shared_namespace:cluster-admin-review-gate") -- this strips the
    category prefixes and humanizes each distinct mechanism, deduped, in
    original order.

    Public (not just this module's own Ledger badge use): also registered
    as the ``humanize_mechanism`` Jinja filter (see portal/app.py) so every
    template rendering a raw ``mechanism`` field -- not just the Ledger
    card this was originally written for -- decodes it the same way,
    instead of leaking the raw routing string.
    """
    if not mechanism:
        return _MECHANISM_SHORT_LABELS["none"]
    labels: list[str] = []
    for part in mechanism.split(","):
        mech = part.split(":", 1)[1] if ":" in part else part
        label = _MECHANISM_SHORT_LABELS.get(mech, mech.replace("-", " ").replace("_", " "))
        if label not in labels:
            labels.append(label)
    return " / ".join(labels)


def _delivery_card(delivery: dict) -> dict:
    humanized = humanize_delivery_mechanism(delivery["mechanism"])
    return {
        "card_type": "F",
        "timestamp": delivery["created_at"],
        "target_app": delivery.get("app_name"),
        "severity": "info",
        "title": humanized,
        "summary": f"{humanized} (verification: {delivery.get('verification', 'unknown')})",
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
    """Backs the rewind scrubber (docs/ledger-design-spec.md ?4) -- the
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
