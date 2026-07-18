"""Aggregates every GitHub PR AgentIT has ever opened for one app, from the
three places a ``pr_url`` can land today (see ``store.py``'s schema comments
on the ``gates``/``deliveries``/``onboarding_results`` tables):

- ``gates.pr_url`` on a ``gitops-pr-pending`` gate -- the
  ``infra-repo-commit`` category's PR (the GitOps infra repo). This is the
  one PR record with a genuinely reliable, already-known outcome with no
  live GitHub call needed: the gate's own ``status`` (``pending`` -> still
  open; ``approved`` -> merged, since approving *is* the merge action per
  ``routes/gates.py::resolve_gate``; ``rejected``/``dismissed``/``expired``
  -> closed without merge).
- ``deliveries.details_json.outcomes.<category>.pr_url`` for the
  ``source-repo-pr``/``app-repo-pr`` mechanisms -- PRs opened directly
  against the app's own code repo (CodeChangeAgent source patches,
  manifest-at-rest files). These carry no outcome tracking of their own;
  only a live GitHub check (``github_pr.get_pr_status()``) can say whether
  one is still open.
- ``onboarding_results.pr_url`` -- the original onboarding PR, plus (when
  "Per-Agent PRs" was used) additional PRs ``|``-joined into the same
  column. Same gap as above: no stored outcome.

The ``cluster_config`` delivery category is deliberately excluded from
``delivery_pr_records()``: its PR is already covered by
``gate_pr_records()`` with a reliably-known outcome, so including it again
here would double-count the same PR under two different confidence levels.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from agentit.portal.delivery import CATEGORY_CLUSTER_CONFIG, repo_kind_for_mechanism

_GITOPS_PR_GATE_TYPE = "gitops-pr-pending"

# The one, real category a gitops-pr-pending gate's PR always belongs to
# (route_and_deliver() only ever creates this gate type for
# CATEGORY_CLUSTER_CONFIG -- see delivery.py) -- gate_pr_records() below
# stamps every record with it so a fleet-wide caller (the Ledger) can
# filter "by category" the same way for gate-sourced and delivery-sourced
# records, without a caller needing to know gates don't carry their own
# category column.
CATEGORY_ONBOARDING = "onboarding"


def _dedup_by_pr_url(records: list[dict]) -> list[dict]:
    """First occurrence wins -- callers pass gate records first, so a
    gate's reliable ``known_state`` always beats a same-URL delivery
    record (in practice the two never collide, since ``delivery_pr_
    records()`` already excludes the one category gates track)."""
    seen: set[str] = set()
    out = []
    for r in records:
        if r["pr_url"] in seen:
            continue
        seen.add(r["pr_url"])
        out.append(r)
    return out


def gate_pr_records(gates: list[dict]) -> list[dict]:
    """Normalize every ``gitops-pr-pending`` gate carrying a ``pr_url`` into
    a PR record whose state is already known -- no live GitHub call needed."""
    records = []
    for g in gates:
        pr_url = g.get("pr_url")
        if g.get("gate_type") != _GITOPS_PR_GATE_TYPE or not pr_url:
            continue
        status = g.get("status")
        if status == "pending":
            state = "open"
        elif status == "approved":
            state = "merged"
        else:
            state = "closed"  # rejected / dismissed / expired
        records.append({
            "pr_url": pr_url,
            "repo_kind": "gitops",
            "source": "gate",
            "category": CATEGORY_CLUSTER_CONFIG,
            "gate_id": g.get("id"),
            "gate_status": status,
            "assessment_id": g.get("assessment_id"),
            "created_at": g.get("created_at"),
            "resolved_at": g.get("resolved_at"),
            "resolved_by": g.get("resolved_by"),
            "known_state": state,
            # The full gate row, e.g. so a fleet-wide caller (the Ledger)
            # can render the exact same Approve & Deliver / Reject card
            # (``_macros.html``'s ``gate_card``) every other pending-gate
            # surface already uses, instead of a second, drifting copy.
            "raw": g,
        })
    return records


def delivery_pr_records(deliveries: list[dict]) -> list[dict]:
    """Normalize ``source-repo-pr``/``app-repo-pr`` delivery outcomes into
    PR records with no known state (a live check is the only way to know)."""
    records = []
    for d in deliveries:
        outcomes = (d.get("details") or {}).get("outcomes") or {}
        cat_to_mechanism: dict[str, str] = {}
        for pair in (d.get("mechanism") or "").split(","):
            if ":" in pair:
                cat, mech = pair.split(":", 1)
                cat_to_mechanism[cat] = mech
        for category, outcome in outcomes.items():
            if category == CATEGORY_CLUSTER_CONFIG or not isinstance(outcome, dict):
                continue
            pr_url = outcome.get("pr_url")
            if not pr_url:
                continue
            mechanism = cat_to_mechanism.get(category, "")
            records.append({
                "pr_url": pr_url,
                "repo_kind": repo_kind_for_mechanism(mechanism) or "code",
                "source": "delivery",
                "delivery_id": d.get("id"),
                "category": category,
                "mechanism": mechanism,
                "assessment_id": d.get("assessment_id"),
                "created_at": d.get("created_at"),
                "known_state": None,
            })
    return records


def onboarding_pr_records(onboardings: list[dict]) -> list[dict]:
    """``onboarding_results.pr_url`` may be a single URL, or several ``|``-
    joined URLs (Per-Agent PRs writes multiple back into this one column --
    see ``routes/assessments.py::create_agent_prs_route``)."""
    records = []
    for ob in onboardings:
        pr_url_field = ob.get("pr_url") or ""
        for pr_url in filter(None, (u.strip() for u in pr_url_field.split("|"))):
            records.append({
                "pr_url": pr_url,
                "repo_kind": "code",
                "source": "onboarding",
                "category": CATEGORY_ONBOARDING,
                "assessment_id": ob.get("assessment_id") or ob.get("id"),
                "created_at": ob.get("created_at"),
                "known_state": None,
            })
    return records


def collect_pr_records(gates: list[dict], deliveries: list[dict], onboardings: list[dict]) -> list[dict]:
    """Every known PR record for one app, newest first, deduped by URL."""
    records = gate_pr_records(gates) + delivery_pr_records(deliveries) + onboarding_pr_records(onboardings)
    records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return _dedup_by_pr_url(records)


async def resolve_pr_states(records: list[dict], status_cache: dict[str, dict] | None = None) -> list[dict]:
    """Fill in ``state``/``merged_at``/``html_url``/``title`` on every
    record, in place, and return it. Gate-tracked records (``known_state``
    already set) never trigger a GitHub call. Everything else is live-
    checked via ``github_pr.get_pr_status()`` -- ``status_cache``, when
    given, is checked first and updated with any freshly-fetched result, so
    a caller checking many apps at once (Fleet) can share one cache/one
    batch of concurrent GitHub calls across every app instead of this
    function re-checking the same PR URL once per app.
    """
    to_check = [
        r["pr_url"] for r in records
        if r["known_state"] is None and (status_cache is None or r["pr_url"] not in status_cache)
    ]
    fresh: dict[str, dict] = {}
    if to_check:
        from agentit.portal.github_pr import get_pr_status
        results = await asyncio.gather(*(asyncio.to_thread(get_pr_status, u) for u in to_check))
        fresh = dict(zip(to_check, results))
        if status_cache is not None:
            status_cache.update(fresh)

    for r in records:
        if r["known_state"] is not None:
            r["state"] = r["known_state"]
            r["html_url"] = r["pr_url"]
            r["merged_at"] = r.get("resolved_at") if r["known_state"] == "merged" else ""
            r["title"] = ""
        else:
            src: dict[str, Any] = (status_cache or fresh).get(r["pr_url"], {})
            r["state"] = src.get("state", "unknown")
            r["html_url"] = src.get("html_url", r["pr_url"])
            r["merged_at"] = src.get("merged_at", "")
            r["title"] = src.get("title", "")
    return records


async def attach_reject_reasons(store: object, app_name: str, records: list[dict]) -> list[dict]:
    """Best-effort correlation of each rejected ``gitops-pr-pending`` gate's
    real rejection reason from ``agent_feedback.human_reason`` --
    ``resolve_gate()`` (``routes/gates.py``) records it there via
    ``record_feedback(finding_category=gate_type, action="rejected")`` in
    the same request that resolves the gate, but ``gates`` has no
    ``reason``/``reject_reason`` column of its own to read it back from
    directly (see ``store.py``'s schema -- a real, currently-unfilled
    wiring gap this correlates around rather than papering over).

    Matched positionally by chronological order (oldest-first), not by
    nearest timestamp: ``resolve_gate()`` writes exactly one feedback row
    per rejected ``gitops-pr-pending`` gate for this app, in the same order
    the gates themselves were rejected, so the Nth rejected gate lines up
    with the Nth ``gitops-pr-pending`` feedback row. Only applies the match
    when the two lists are the same length -- if they ever disagree (e.g. a
    gate rejected before this correlation existed, or a feedback row from
    some other path), this leaves ``reject_reason`` unset rather than
    risking a wrong pairing.
    """
    rejected = [r for r in records if r.get("source") == "gate" and r.get("gate_status") == "rejected"]
    if not rejected or not hasattr(store, "get_feedback_for_app"):
        return records
    feedback = await store.get_feedback_for_app(app_name, finding_category=_GITOPS_PR_GATE_TYPE)
    feedback = [f for f in feedback if f.get("action") == "rejected"]
    if len(feedback) != len(rejected):
        return records
    rejected_oldest_first = sorted(rejected, key=lambda r: r.get("resolved_at") or "")
    feedback_oldest_first = sorted(feedback, key=lambda f: f.get("created_at") or "")
    for gate_record, fb in zip(rejected_oldest_first, feedback_oldest_first):
        gate_record["reject_reason"] = fb.get("human_reason") or ""
    return records


async def get_app_pr_history(store: object, assessment_id: str, repo_url: str, app_name: str) -> list[dict]:
    """Every PR ever opened for this app (across every historical
    assessment), with live state resolved for anything not already known
    from a gate. Scoped to one app -- safe to live-check inline per
    request, mirroring the existing per-assessment ``get_pr_status()``
    precedent (``routes/assessments.py``'s onboarding-history / capability-
    run-detail pages) rather than the fleet-wide batched+cached path
    ``routes/fleet.py`` uses for the whole Fleet list."""
    gates = await store.list_gates_for_assessment(assessment_id) if hasattr(store, "list_gates_for_assessment") else []
    deliveries = await store.list_deliveries_for_app(app_name) if hasattr(store, "list_deliveries_for_app") else []
    onboardings = await store.list_onboardings_for_repo(repo_url) if hasattr(store, "list_onboardings_for_repo") else []
    records = collect_pr_records(gates, deliveries, onboardings)
    records = await attach_reject_reasons(store, app_name, records)
    return await resolve_pr_states(records)


# ── Fleet-wide PR lifecycle (the Ledger) ───────────────────────────────────
#
# The Ledger's whole purpose (per product direction) is a cross-app list of
# PRs that need attention, plus each PR's real lifecycle -- waiting for
# approval, merged, rejected (with the real reason), or just open -- not a
# generic event log. Everything below builds on the exact same per-app
# primitives above; only the fleet-wide fan-out + one shared, batched live-
# GitHub-check cache (mirroring ``routes/fleet.py``'s own
# ``_pr_status_cache`` for its "Open PRs" column, kept separate rather than
# shared so this module's cache lifetime is independent of Fleet's) are new.

LIFECYCLE_NEEDS_APPROVAL = "needs_approval"
LIFECYCLE_OPEN = "open"
LIFECYCLE_MERGED = "merged"
LIFECYCLE_REJECTED = "rejected"
LIFECYCLE_CLOSED = "closed"
LIFECYCLE_UNKNOWN = "unknown"

_LIFECYCLE_LABELS: dict[str, str] = {
    LIFECYCLE_NEEDS_APPROVAL: "Waiting for your approval",
    LIFECYCLE_OPEN: "Open",
    LIFECYCLE_MERGED: "Merged",
    LIFECYCLE_REJECTED: "Rejected",
    LIFECYCLE_CLOSED: "Closed",
    LIFECYCLE_UNKNOWN: "Unknown",
}


def annotate_lifecycle(record: dict) -> dict:
    """Add ``lifecycle``/``lifecycle_label``/``needs_attention`` to a PR
    record already run through ``resolve_pr_states()`` (i.e. it has
    ``state``). "Needs approval" is deliberately narrower than "open": it's
    only true for a gate-tracked (``source == "gate"``) record whose gate
    is still ``pending`` -- the one case where a human must act *inside
    AgentIT* (Approve & Deliver / Reject) before this PR can merge.
    ``source-repo-pr``/``app-repo-pr``/onboarding PRs are never gated --
    review/merge for those happens directly on GitHub, outside AgentIT's
    own approval step -- so an "open" one of those is real, but never
    "needs approval" in this sense.
    """
    is_gate = record.get("source") == "gate"
    gate_status = record.get("gate_status")
    if is_gate and gate_status == "pending":
        lifecycle = LIFECYCLE_NEEDS_APPROVAL
    elif record.get("state") == "merged":
        lifecycle = LIFECYCLE_MERGED
    elif is_gate and gate_status == "rejected":
        lifecycle = LIFECYCLE_REJECTED
    elif record.get("state") == "open":
        lifecycle = LIFECYCLE_OPEN
    elif record.get("state") == "closed":
        lifecycle = LIFECYCLE_CLOSED
    else:
        lifecycle = LIFECYCLE_UNKNOWN
    record["lifecycle"] = lifecycle
    record["lifecycle_label"] = _LIFECYCLE_LABELS[lifecycle]
    record["needs_attention"] = lifecycle == LIFECYCLE_NEEDS_APPROVAL
    return record


_FLEET_PR_CACHE_TTL = 120  # seconds -- mirrors fleet.py's own PR-status cache TTL.
_fleet_pr_status_cache: dict[str, Any] = {"data": {}, "ts": 0.0}
_fleet_pr_status_cache_lock = threading.Lock()


async def collect_fleet_pr_records(store: object, fleet: list[dict] | None = None) -> list[dict]:
    """Every known PR record across the WHOLE fleet, newest first, each
    tagged with its owning app (``app_name``/``repo_url``) and a resolved
    ``lifecycle`` -- the fleet-wide sibling of ``get_app_pr_history()``,
    backing the Ledger's PR list. Reuses the exact three fleet-wide,
    one-query-each accessors ``routes/fleet.py``'s "Open PRs"/"Total PRs"
    columns already established (``list_all_gates``/``list_all_deliveries``/
    ``list_all_onboarding_pr_urls``), so no new store query shape is
    introduced -- only a second, independent consumer of data that already
    flows through this module.

    Live GitHub calls (for the ``source-repo-pr``/``app-repo-pr``/onboarding
    records with no stored outcome) are batched into one round across the
    entire fleet and cached for ``_FLEET_PR_CACHE_TTL`` seconds, the same
    "one round per TTL window, not one call per PR per request" shape
    ``routes/fleet.py::_attach_pr_counts`` uses -- kept as this module's own
    cache (not shared with Fleet's) so the two pages' cache lifetimes never
    interfere with each other.
    """
    if fleet is None:
        fleet = await store.get_fleet_data()

    try:
        all_gates = await store.list_all_gates()
    except Exception:
        all_gates = []
    try:
        all_deliveries = await store.list_all_deliveries(limit=5000)
    except Exception:
        all_deliveries = []
    try:
        all_onboarding_prs = (
            await store.list_all_onboarding_pr_urls() if hasattr(store, "list_all_onboarding_pr_urls") else []
        )
    except Exception:
        all_onboarding_prs = []

    gates_by_repo: dict[str, list[dict]] = {}
    for g in all_gates:
        if g.get("repo_url"):
            gates_by_repo.setdefault(g["repo_url"], []).append(g)
    deliveries_by_app: dict[str, list[dict]] = {}
    for d in all_deliveries:
        if d.get("app_name"):
            deliveries_by_app.setdefault(d["app_name"], []).append(d)
    onboardings_by_repo: dict[str, list[dict]] = {}
    for ob in all_onboarding_prs:
        if ob.get("repo_url"):
            onboardings_by_repo.setdefault(ob["repo_url"], []).append(ob)

    all_records: list[dict] = []
    for app_item in fleet:
        records = collect_pr_records(
            gates_by_repo.get(app_item["repo_url"], []),
            deliveries_by_app.get(app_item["repo_name"], []),
            onboardings_by_repo.get(app_item["repo_url"], []),
        )
        records = await attach_reject_reasons(store, app_item["repo_name"], records)
        for r in records:
            r["app_name"] = app_item["repo_name"]
            r["repo_url"] = app_item["repo_url"]
        all_records.extend(records)

    now = time.monotonic()
    with _fleet_pr_status_cache_lock:
        cache_fresh = (now - _fleet_pr_status_cache["ts"]) < _FLEET_PR_CACHE_TTL
        status_cache = _fleet_pr_status_cache["data"] if cache_fresh else {}
        if not cache_fresh:
            _fleet_pr_status_cache["data"] = status_cache
            _fleet_pr_status_cache["ts"] = now

    unresolved_urls = list({
        r["pr_url"] for r in all_records
        if r["known_state"] is None and r["pr_url"] not in status_cache
    })
    if unresolved_urls:
        from agentit.portal.github_pr import get_pr_status
        results = await asyncio.gather(*(asyncio.to_thread(get_pr_status, u) for u in unresolved_urls))
        status_cache.update(dict(zip(unresolved_urls, results)))

    await resolve_pr_states(all_records, status_cache=status_cache)
    for r in all_records:
        annotate_lifecycle(r)
    all_records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return all_records
