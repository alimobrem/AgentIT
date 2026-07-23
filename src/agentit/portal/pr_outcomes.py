"""Durable, queryable outcomes for real GitHub PRs AgentIT opened -- the
data-capture half of the gates-removal directive (docs/README.md changelog,
2026-07-19): "if a PR is ever rejected, or changed, the system needs to keep
track of that and learn from it."

Two real, GitHub-derived facts get recorded, once, per PR:

- **Rejected**: the PR was closed without merging. The close reason is
  parsed from the PR's own comment thread the exact same way
  ``capability_scout.py`` already does for skill-proposal rejections
  (``fetch_pr_close_comments`` + ``parse_reject_reason`` -- reused verbatim
  here, not reinvented) -- every real PR closed-without-merge in this
  codebase's history explained itself in a plain comment, never a label or
  body convention alone.
- **Edited before merge**: the PR was merged, but it ended up with more
  commits than the single one AgentIT itself made when opening it (every
  ``github_pr.py`` PR-opening function makes exactly one commit before
  opening its PR) -- i.e. a human pushed changes to the branch before
  merging. ``github_pr.get_pr_extra_commits()`` returns those extra
  commits' own diffs as the captured evidence.

Recorded via ``store.record_pr_outcome()``, which is idempotent per
``pr_url`` (a row is written at most once) -- this module's ``sync_pr_
outcomes()`` is safe to call on every already-live-checked PR record on
every Fleet/Ledger/Assessment-Detail page load (via pr_tracking.py): the one
batched ``pr_outcomes_recorded_for()`` query means the real (bounded but
non-trivial) GitHub calls this module makes only ever fire once per PR,
the first time it's observed closed/merged.

Nothing here builds the "learn from this" logic itself (out of scope, a
separate future task per the product directive) -- this only guarantees the
raw evidence is captured durably and queryably (``store.get_pr_outcome()``/
``store.get_pr_outcomes_for_urls()``), not thrown away after being displayed
once. (The fleet-wide/filtered ``store.list_pr_outcomes()`` this module
originally shipped alongside was deleted 2026-07-20 -- an architecture
review found it had no caller anywhere, including the "future learning
mechanism" this docstring used to promise, which was never built; nothing
here loses evidence-capture as a result, since every PR outcome row is
still written and still individually queryable by URL.)
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

OUTCOME_REJECTED = "rejected"
OUTCOME_EDITED_BEFORE_MERGE = "edited_before_merge"
OUTCOME_MERGED = "merged"


async def _attribution(store: object, record: dict) -> tuple[str, list[str]]:
    """Best-effort ``(finding_category, skill_names)`` for the content this
    PR delivered -- the two dimensions a future learning mechanism needs to
    query by. ``finding_category`` comes from the owning delivery's own
    ``target_findings`` (Phase 3/4's existing per-finding tracking, see
    delivery.py), when set. ``skill_names`` prefer the SOLUTION_CONTRACT
    skill for those findings (source-patch PRs never put skill YAML on the
    assessment). Only when no target findings are recorded do we fall back
    to path recovery from onboarding ``category=="skills"`` files — and
    even then only for cluster-shaped deliveries (not source_patch), because
    assessment-wide companion skill YAML is not "what this PR delivered".
    """
    finding_category = ""
    # pr_tracking delivery records put target_findings on the record itself;
    # some callers nest under raw — accept either.
    target_findings: list = list(record.get("target_findings") or [])
    raw = record.get("raw")
    if not target_findings and isinstance(raw, dict):
        target_findings = list(raw.get("target_findings") or [])
    if target_findings:
        first = target_findings[0]
        if isinstance(first, (list, tuple)) and first:
            finding_category = str(first[0])

    if target_findings:
        from agentit.skill_engine import skill_names_for_findings

        return finding_category, skill_names_for_findings(target_findings)

    # No finding scope — do not blast every companion skill on the assessment
    # for source-patch PRs (codechange only; skill YAML companions are unrelated).
    if (record.get("category") or "") == "source_patch":
        return finding_category, []

    skill_names: list[str] = []
    assessment_id = record.get("assessment_id")
    if assessment_id and hasattr(store, "get_onboarding"):
        try:
            files = await store.get_onboarding(assessment_id)
        except Exception:
            files = None
        if files:
            from agentit.skill_engine import skill_name_from_path

            app_name = record.get("app_name", "")
            sanitized = app_name.lower().replace("_", "-").replace(".", "-")
            for f in files:
                if f.get("category") != "skills":
                    continue
                name = skill_name_from_path(f.get("path", ""), sanitized)
                if name and name not in skill_names:
                    skill_names.append(name)
    return finding_category, skill_names


async def _compute_outcome(
    record: dict, *, get_status, get_extra_commits, get_comments,
) -> dict | None:
    """One PR record (already resolved to a real ``state`` via pr_tracking's
    ``resolve_pr_states()``) -> the outcome dict to record, or ``None`` when
    there's nothing worth recording (e.g. a merge with no extra commits --
    it shipped exactly as proposed)."""
    pr_url = record["pr_url"]
    state = record.get("state")

    if state == "merged":
        # Phase E: always record a provisional merged outcome so learning can
        # distinguish "opened" from "human accepted". Extra commits still
        # upgrade the durable row to edited_before_merge when present.
        extra_commits = await asyncio.to_thread(get_extra_commits, pr_url)
        if extra_commits:
            return {"outcome": OUTCOME_EDITED_BEFORE_MERGE, "edit_diff": extra_commits}
        return {"outcome": OUTCOME_MERGED, "edit_diff": []}

    if state == "closed":
        status = await asyncio.to_thread(get_status, pr_url)
        comments = await asyncio.to_thread(get_comments, pr_url)
        from agentit.capability_scout import parse_reject_reason

        reject_reason = parse_reject_reason(
            (status or {}).get("labels") or [], (status or {}).get("body") or "", comments=comments,
        )
        return {"outcome": OUTCOME_REJECTED, "reject_reason": reject_reason}

    return None


async def _record_rejection_side_effects(
    store: object,
    record: dict,
    reject_reason: str,
    *,
    finding_category: str = "",
    skill_names: list[str] | None = None,
) -> None:
    """Mirrors what ``routes/gates.py``'s old reject branch used to do on a
    human's in-app "Reject" click, now fired from the real PR-close signal
    instead: writes ``agent_feedback`` (``get_rejection_count()`` --
    webhooks.py's auto-fixable dispatch loop still reads this to back off a
    category rejected 3+ times -- must keep seeing real rejection signal) and
    per-skill ``skill_effectiveness`` outcomes for skills this PR actually
    covered (never every skill YAML on the assessment).
    """
    app_name = record.get("app_name", "")
    category = record.get("category", "")
    reason = (reject_reason or "").strip() or "PR closed without merge"
    if hasattr(store, "record_feedback"):
        try:
            await store.record_feedback(
                app_name=app_name, agent_name="pr-outcome-sync",
                finding_category=category, action="rejected", human_reason=reason,
            )
        except Exception:
            logger.warning("Failed to record agent_feedback for rejected PR %s", record.get("pr_url"), exc_info=True)

    from agentit.skill_engine import record_skill_outcomes_for_findings

    finding_keys: list[tuple[str, str]] = []
    cat = (finding_category or "").strip()
    if cat:
        finding_keys = [(cat, "")]
    # When finding_category is known, contract mapping wins — skill_names
    # recovered from assessment-wide onboarding companions must not override.
    await record_skill_outcomes_for_findings(
        store, app_name, finding_keys, "rejected", reason,
        skill_names=None if cat else skill_names,
    )


async def attach_pr_outcomes(store: object, records: list[dict]) -> list[dict]:
    """Attach ``reject_reason``/``edited_before_merge``/``edit_diff`` onto
    every record in ``records`` that has a durably-recorded ``pr_outcomes``
    row -- one batched query, so a caller enriching many PR records at once
    (Fleet-wide or one app's history) never issues one query per record.
    Call ``sync_pr_outcomes()`` first so a newly-detected outcome (this same
    page load) is already in the table by the time this runs.
    """
    if not hasattr(store, "get_pr_outcomes_for_urls"):
        return records
    urls = [r["pr_url"] for r in records if r.get("pr_url")]
    outcomes = await store.get_pr_outcomes_for_urls(urls)
    for record in records:
        outcome = outcomes.get(record.get("pr_url"))
        if outcome is None:
            continue
        record["reject_reason"] = outcome.get("reject_reason") or ""
        record["edited_before_merge"] = outcome["outcome"] == OUTCOME_EDITED_BEFORE_MERGE
        record["edit_diff"] = outcome.get("edit_diff") or []
    return records


async def sync_pr_outcomes(
    store: object,
    records: list[dict],
    *,
    get_status=None,
    get_extra_commits=None,
    get_comments=None,
) -> list[dict]:
    """For every already-live-checked ``records`` entry (pr_tracking.py's
    shape, post ``resolve_pr_states()``) that's merged or closed and has no
    ``pr_outcomes`` row yet, detect and durably record its real outcome.
    Returns only the newly-recorded outcomes.

    Safe to call on every page load that already live-checks PR state --
    the one batched ``pr_outcomes_recorded_for()`` query up front means a
    PR only ever triggers the real (extra) GitHub calls this makes once,
    the first time it's observed closed/merged.
    """
    if get_status is None:
        from agentit.portal.github_pr import get_pr_status as get_status
    if get_extra_commits is None:
        from agentit.portal.github_pr import get_pr_extra_commits as get_extra_commits
    if get_comments is None:
        from agentit.capability_scout import fetch_pr_close_comments as get_comments

    candidates = [r for r in records if r.get("state") in ("merged", "closed") and r.get("pr_url")]
    if not candidates or not hasattr(store, "pr_outcomes_recorded_for"):
        return []

    already = await store.pr_outcomes_recorded_for([r["pr_url"] for r in candidates])
    newly: list[dict] = []
    for record in candidates:
        pr_url = record["pr_url"]
        if pr_url in already:
            continue
        try:
            outcome = await _compute_outcome(
                record, get_status=get_status, get_extra_commits=get_extra_commits, get_comments=get_comments,
            )
        except Exception:
            logger.warning("Failed to compute PR outcome for %s", pr_url, exc_info=True)
            continue
        if outcome is None:
            continue

        finding_category, skill_names = await _attribution(store, record)
        recorded_id = await store.record_pr_outcome(
            pr_url, record.get("app_name", ""), outcome["outcome"],
            assessment_id=record.get("assessment_id"), category=record.get("category", ""),
            finding_category=finding_category, skill_names=skill_names,
            reject_reason=outcome.get("reject_reason", ""), edit_diff=outcome.get("edit_diff", []),
        )
        if recorded_id is None:
            continue

        if outcome["outcome"] == OUTCOME_REJECTED:
            await _record_rejection_side_effects(
                store, record, outcome.get("reject_reason", ""),
                finding_category=finding_category, skill_names=skill_names,
            )

        newly.append({**outcome, "pr_url": pr_url, "app_name": record.get("app_name", "")})
    return newly
