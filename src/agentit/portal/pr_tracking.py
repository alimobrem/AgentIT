"""Aggregates every GitHub PR AgentIT has ever opened for one app, from the
two places a ``pr_url`` can land today (see ``store.py``'s schema comments
on the ``deliveries``/``onboarding_results`` tables):

- ``deliveries.details_json.outcomes.<category>.pr_url`` -- every delivery
  category now opens its PR this way, including ``cluster_config``/
  ``cicd_shared_namespace`` (the GitOps infra-repo commit) alongside
  ``source-repo-pr``/``app-repo-pr`` (PRs opened directly against the app's
  own code repo). None of these carry any outcome tracking of their own --
  a live GitHub check (``github_pr.get_pr_status()``) is the only way to
  know whether one is still open. (Before the ``gates`` table was removed
  entirely, ``cluster_config``/``cicd_shared_namespace`` used to be tracked
  via a ``gitops-pr-pending``/``-shared-namespace`` gate row instead, whose
  own ``status`` column doubled as a "reliably-known outcome" -- that
  gate-status proxy is exactly what went stale and undercounted real open
  PRs; deriving everything from a live GitHub check instead is the fix.)
- ``onboarding_results.pr_url`` -- the original onboarding PR, plus (when
  "Per-Agent PRs" was used) additional PRs ``|``-joined into the same
  column. Same shape as above: no stored outcome.

Every PR's real close reason (when closed without merging) or pre-merge
edit (when merged with extra commits a human pushed) is captured durably in
``pr_outcomes`` (see ``pr_outcomes.py``) the first time it's observed
closed/merged -- ``attach_pr_outcomes()`` below reads that table back onto
each record; ``sync_pr_outcomes()`` (called first) is what populates it.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from agentit.portal.delivery import repo_kind_for_mechanism

CATEGORY_ONBOARDING = "onboarding"


def _dedup_by_pr_url(records: list[dict]) -> list[dict]:
    """First occurrence wins -- callers pass records newest-first, so this
    keeps the most recently created record for any ``pr_url`` that
    (unexpectedly) shows up more than once."""
    seen: set[str] = set()
    out = []
    for r in records:
        if r["pr_url"] in seen:
            continue
        seen.add(r["pr_url"])
        out.append(r)
    return out


def delivery_pr_records(deliveries: list[dict]) -> list[dict]:
    """Normalize every delivery outcome carrying a ``pr_url`` -- every
    category, including ``cluster_config``/``cicd_shared_namespace`` (the
    GitOps infra-repo commit) -- into a PR record with no known state; a
    live GitHub check is the only way to know one's real state."""
    records = []
    for d in deliveries:
        outcomes = (d.get("details") or {}).get("outcomes") or {}
        cat_to_mechanism: dict[str, str] = {}
        for pair in (d.get("mechanism") or "").split(","):
            if ":" in pair:
                cat, mech = pair.split(":", 1)
                cat_to_mechanism[cat] = mech
        for category, outcome in outcomes.items():
            if not isinstance(outcome, dict):
                continue
            pr_url = outcome.get("pr_url")
            if not pr_url:
                continue
            mechanism = cat_to_mechanism.get(category, "")
            warnings = outcome.get("dry_run_warnings") or []
            records.append({
                "pr_url": pr_url,
                "repo_kind": repo_kind_for_mechanism(mechanism) or "code",
                "source": "delivery",
                "delivery_id": d.get("id"),
                "category": category,
                "mechanism": mechanism,
                "assessment_id": d.get("assessment_id"),
                "created_at": d.get("created_at"),
                "target_findings": d.get("target_findings") or [],
                "dry_run_warnings": warnings,
                "validation_summary": outcome.get("validation_summary") or "",
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


def collect_pr_records(deliveries: list[dict], onboardings: list[dict]) -> list[dict]:
    """Every known PR record for one app, newest first, deduped by URL."""
    records = delivery_pr_records(deliveries) + onboarding_pr_records(onboardings)
    records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return _dedup_by_pr_url(records)


async def resolve_pr_states(records: list[dict], status_cache: dict[str, dict] | None = None) -> list[dict]:
    """Fill in ``state``/``merged_at``/``html_url``/``title`` on every
    record, in place, and return it, via a live GitHub check
    (``github_pr.get_pr_status()``) -- ``status_cache``, when given, is
    checked first and updated with any freshly-fetched result, so a caller
    checking many apps at once (Fleet) can share one cache/one batch of
    concurrent GitHub calls across every app instead of this function
    re-checking the same PR URL once per app.
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


async def sync_and_attach_pr_outcomes(store: object, records: list[dict]) -> list[dict]:
    """Real, durable rejection-reason/pre-merge-edit capture (see
    ``pr_outcomes.py``), run for every record already resolved to a real
    ``state`` (``resolve_pr_states()`` must run first): detect and persist
    any newly-observed closed/merged PR's outcome, then attach every known
    outcome (old or just-recorded) onto its matching record as
    ``reject_reason``/``edited_before_merge``/``edit_diff``. Both steps are
    best-effort -- a failure here (e.g. store missing the new methods in a
    test double) must never block rendering the records themselves.
    """
    from agentit.portal.pr_outcomes import attach_pr_outcomes, sync_pr_outcomes
    try:
        await sync_pr_outcomes(store, records)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Failed to sync PR outcomes", exc_info=True)
    return await attach_pr_outcomes(store, records)


async def get_app_pr_history(store: object, assessment_id: str, repo_url: str, app_name: str) -> list[dict]:
    """Every PR ever opened for this app (across every historical
    assessment), with live state resolved via a real GitHub check. Scoped
    to one app -- safe to live-check inline per request, mirroring the
    existing per-assessment ``get_pr_status()`` precedent (``routes/
    assessments.py``'s onboarding-history / capability-run-detail pages)
    rather than the fleet-wide batched+cached path ``routes/fleet.py`` uses
    for the whole Fleet list.

    Each record also gets ``annotate_lifecycle()``'s ``lifecycle``/
    ``lifecycle_label``/``needs_attention`` fields -- the same ones
    ``collect_fleet_pr_records()`` attaches for the fleet-wide Ledger -- so
    Assessment Detail's own PR list (part of its Ledger tab) renders
    identical lifecycle badges/labels instead of re-deriving "Open"/
    "Merged"/"Closed" from ``state`` a second, drifting way.
    """
    deliveries = await store.list_deliveries_for_app(app_name) if hasattr(store, "list_deliveries_for_app") else []
    onboardings = await store.list_onboardings_for_repo(repo_url) if hasattr(store, "list_onboardings_for_repo") else []
    records = collect_pr_records(deliveries, onboardings)
    await resolve_pr_states(records)
    records = await sync_and_attach_pr_outcomes(store, records)
    out = []
    for r in records:
        annotate_lifecycle(r)
        enrich_decision_card(r)
        out.append(r)
    return out


# ── Fleet-wide PR lifecycle (the Ledger) ───────────────────────────────────
#
# The Ledger's whole purpose (per product direction) is a cross-app list of
# PRs that need attention, plus each PR's real lifecycle -- waiting for
# approval, merged, rejected (with the real reason), or closed -- not a
# generic event log. Everything below builds on the exact same per-app
# primitives above; only the fleet-wide fan-out + one shared, batched live-
# GitHub-check cache (mirroring ``routes/fleet.py``'s own
# ``_pr_status_cache`` for its "Open PRs" column, kept separate rather than
# shared so this module's cache lifetime is independent of Fleet's) are new.

LIFECYCLE_NEEDS_APPROVAL = "needs_approval"
LIFECYCLE_MERGED = "merged"
LIFECYCLE_REJECTED = "rejected"
LIFECYCLE_CLOSED = "closed"
LIFECYCLE_UNKNOWN = "unknown"

_LIFECYCLE_LABELS: dict[str, str] = {
    LIFECYCLE_NEEDS_APPROVAL: "Waiting for your approval",
    LIFECYCLE_MERGED: "Merged",
    LIFECYCLE_REJECTED: "Rejected",
    LIFECYCLE_CLOSED: "Closed",
    LIFECYCLE_UNKNOWN: "Unknown",
}



def enrich_decision_card(record: dict) -> dict:
    """Fill why · confidence · dry-run · evidence fields for the decision card.

    Idempotent; safe to call on Ledger and Assessment Detail records.
    Confidence is derived from solution-contract / dry-run signals already
    on the record (never invented).
    """
    targets = record.get("target_findings") or []
    contract_lines = list(record.get("contract_lines") or [])
    if not contract_lines and targets:
        try:
            from agentit.remediation.clear_evidence import contract_lines_for_portal
            contract_lines = list(contract_lines_for_portal(targets) or [])
            record["contract_lines"] = contract_lines
        except (ImportError, TypeError, ValueError):
            pass
    warnings = record.get("dry_run_warnings") or []
    validation = (record.get("validation_summary") or "").strip()

    if contract_lines and not warnings:
        confidence = "high"
        confidence_label = "High — finding-clear contract + clean dry-run gate"
    elif contract_lines or targets:
        confidence = "medium"
        confidence_label = "Medium — targeted findings; review the PR diff"
    else:
        confidence = "low"
        confidence_label = "Low — no finding-clear contract on this PR"

    if validation:
        dry_run_label = validation.split("\n", 1)[0][:180]
    elif warnings:
        dry_run_label = (
            "SSA dry-run soft warnings only (non-blocking): "
            + "; ".join(str(w) for w in warnings[:2])
        )
    elif record.get("source") == "delivery":
        dry_run_label = "Scan gate: SSA dry-run + clear-evidence passed before open"
    else:
        dry_run_label = "No separate dry-run row — review the PR diff before merge"

    why = record.get("decision_why")
    if not why:
        cat = record.get("category") or "remediation"
        if targets:
            why = f"Scan remediation for {cat} — clears {', '.join(str(t) for t in targets[:5])}"
        else:
            why = f"Scan remediation for {cat}"

    evidence = list(contract_lines)
    if not evidence and targets:
        evidence = [f"Target finding: {t}" for t in targets[:8]]

    record["decision_why"] = why
    record["confidence"] = confidence
    record["confidence_label"] = confidence_label
    record["dry_run_label"] = dry_run_label
    record["evidence_lines"] = evidence
    return record


def annotate_lifecycle(record: dict) -> dict:
    """Add ``lifecycle`` / ``lifecycle_label`` / ``needs_attention``.

    Requires ``resolve_pr_states()`` (and outcomes when available). Open
    unmerged PRs need approval — merge/close on GitHub is the HITL step.
    See docs/adr/0001-gitops-scan-hitl.md.
    """
    state = record.get("state")
    if state == "open":
        lifecycle = LIFECYCLE_NEEDS_APPROVAL
    elif state == "merged":
        lifecycle = LIFECYCLE_MERGED
    elif state == "closed":
        lifecycle = LIFECYCLE_REJECTED if record.get("reject_reason") else LIFECYCLE_CLOSED
    else:
        lifecycle = LIFECYCLE_UNKNOWN
    record["lifecycle"] = lifecycle
    record["lifecycle_label"] = _LIFECYCLE_LABELS[lifecycle]
    record["needs_attention"] = lifecycle == LIFECYCLE_NEEDS_APPROVAL
    return record


def fleet_prs_waiting_for_approval(records: list[dict]) -> list[dict]:
    """Every currently open, unmerged PR across the fleet -- the Ledger's
    "Waiting for your approval" bucket (and base.html's nav badge / Fleet's
    pointer banner, which must always agree with it). Equivalent to
    ``[r for r in records if r["needs_attention"]]`` once ``annotate_
    lifecycle()`` has run over every record -- kept as its own function
    (rather than inlined at each of the three call sites) so all three stay
    trivially in agreement.
    """
    return [r for r in records if r["state"] == "open"]


async def count_fleet_prs_waiting_for_approval(store: object) -> int:
    """Count-only sibling of ``fleet_prs_waiting_for_approval()`` for
    callers (the nav badge) that don't need full records -- still goes
    through ``collect_fleet_pr_records()`` so it shares that function's
    queries and fleet-wide GitHub-status cache rather than a second,
    drifting computation."""
    records = await collect_fleet_pr_records(store)
    return len(fleet_prs_waiting_for_approval(records))


_FLEET_PR_CACHE_TTL = 120  # seconds -- mirrors fleet.py's own PR-status cache TTL.
_fleet_pr_status_cache: dict[str, Any] = {"data": {}, "ts": 0.0}
_fleet_pr_status_cache_lock = threading.Lock()


async def collect_fleet_pr_records(store: object, fleet: list[dict] | None = None) -> list[dict]:
    """Every known PR record across the WHOLE fleet, newest first, each
    tagged with its owning app (``app_name``/``repo_url``) and a resolved
    ``lifecycle`` -- the fleet-wide sibling of ``get_app_pr_history()``,
    backing the Ledger's PR list. Reuses the exact two fleet-wide,
    one-query-each accessors ``routes/fleet.py``'s "Open PRs"/"Total PRs"
    columns already established (``list_all_deliveries``/
    ``list_all_onboarding_pr_urls``), so no new store query shape is
    introduced -- only a second, independent consumer of data that already
    flows through this module.

    Live GitHub calls (for every record, since none carry a stored outcome
    of their own any more) are batched into one round across the entire
    fleet and cached for ``_FLEET_PR_CACHE_TTL`` seconds, the same "one
    round per TTL window, not one call per PR per request" shape
    ``routes/fleet.py::_attach_pr_counts`` uses -- kept as this module's own
    cache (not shared with Fleet's) so the two pages' cache lifetimes never
    interfere with each other.
    """
    if fleet is None:
        fleet = await store.get_fleet_data()

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
            deliveries_by_app.get(app_item["repo_name"], []),
            onboardings_by_repo.get(app_item["repo_url"], []),
        )
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
    await sync_and_attach_pr_outcomes(store, all_records)
    for r in all_records:
        annotate_lifecycle(r)
        enrich_decision_card(r)
    all_records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return all_records
