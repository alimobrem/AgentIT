"""Automates the mechanical pre-PR work that used to require a human to
click Dry Run, then Deliver, by hand -- without reintroducing AutoMode.

This is NOT the removed ``delivery.auto_dry_run_then_deliver()`` chain
(``fc0a7e9``) come back under a new name: that function ran Dry Run once,
then Deliver once, and simply halted with no PR if the dry run reported any
error. This module instead runs a bounded validate -> fix -> re-validate
loop (docs/onboarding-loop-vision-gap-analysis.md's Part 3 "LLM manifest-
review gate", finally built, plus a genuine fix-and-retry step that
proposal never specified), then one final LLM quality/completeness review,
before calling the real ``route_and_deliver()``. It is also NOT AutoMode:
AutoMode's ``classify_action``/``should_auto_apply`` decided *whether a
human review could be skipped* for a batch already known to be structurally
fine; nothing here ever skips a human review -- the resulting PR (and its
merge on GitHub) remains the one human gate every delivery in this app has
had since AutoMode's removal. What this automates is strictly the
mechanical work that happens BEFORE that PR exists: validating the
generated manifests, fixing what's fixable via the same regeneration
machinery a human would otherwise click "Fix" for, and asking an LLM for one
last look -- so a human isn't the one clicking Dry Run, discovering an
error, going and clicking Fix, then clicking Dry Run again by hand.

Deliberately does not create or depend on any ``gates`` row for its own
"PR is ready" signal (the ``gates`` table/concept is being removed
system-wide by a separate effort) -- ``notify_pr_ready()`` below sources PR
URLs directly from ``route_and_deliver()``'s own returned ``outcomes``,
the same real PR-creation result Ledger's PR list is (being fixed to be)
sourced from via ``pr_tracking.py``, not from any gate query. The real,
non-dry-run ``route_and_deliver()`` call this module makes may still create
a ``gates`` row as an existing side effect of its own GitOps-commit
mechanism (``_deliver_via_gitops_pr_and_gate()``) -- that is unchanged and
out of this module's scope; the dedicated gate-removal effort owns that.
"""
from __future__ import annotations

import logging

from agentit.models import AssessmentReport

logger = logging.getLogger(__name__)

# A sane bound on how many times this will attempt validate -> fix ->
# re-validate before giving up honestly and surfacing whatever is still
# wrong to a human -- never an unbounded retry loop, mirroring this
# codebase's existing precedent for "how many auto-attempts before backing
# off" (delivery.py's FINDING_ESCALATION_THRESHOLD, skill_learner.py's
# improvement_cooldown_attempts).
MAX_VALIDATION_ITERATIONS = 3

# property_verifier.verify_all_properties()'s four checks are the one
# genuinely structural "does the generated content actually satisfy this
# property" signal that exists today (route_and_deliver(dry_run=True) only
# ever reports delivery-mechanism-blocking errors -- missing GitOps
# registration, a Secret, an unresolved placeholder -- never "is this
# manifest correct"). Maps each check's ``property_name`` to the finding
# category RemediationDispatcher/FIX_REGISTRY already knows how to
# regenerate a fix for, so a failed property can actually be retried with
# the SAME machinery a human's "Fix" button on Assessment Detail uses,
# rather than a second, parallel fix mechanism.
_PROPERTY_TO_FIX_CATEGORY: dict[str, str] = {
    "Network Isolation": "network",
    "RBAC": "rbac",
    "Autoscaling": "autoscaling",
    "Monitoring": "monitoring",
}


def _assessment_has_finding_category(report: AssessmentReport | None, category: str) -> bool:
    """Whether ``report`` actually flagged something in ``category`` as a
    finding. Gates every auto-fix attempt below on this: property_verifier's
    checks are a blanket "does this app have RBAC/HPA/a ServiceMonitor/a
    NetworkPolicy at all" scan, not scoped to this assessment's real
    findings -- without this gate, an app that was never flagged for (say)
    missing RBAC would still get an RBAC fix silently injected into every
    single onboarding, just because ``verify_all_properties()`` always
    checks for all four properties regardless of what was actually asked
    for. Reuses the same normalization ``remediation.registry.lookup()``
    already applies, so "this category is fixable" and "this category was
    actually a finding" can never disagree on what counts as a match.
    """
    if report is None:
        return False
    cat = category.lower().replace(" ", "_").replace("-", "_")
    for score in report.scores:
        for f in score.findings:
            f_cat = f.category.lower().replace(" ", "_").replace("-", "_")
            if cat in f_cat or f_cat in cat:
                return True
    return False


def _merge_fix_files(current_files: list[dict], new_files: list[dict]) -> list[dict]:
    """Merge a dispatcher's freshly-regenerated fix files into the current
    onboarding batch: a fix for a ``path`` the batch already carries
    replaces that file in place (the same shape ``update_onboarding_file()``
    already produces for a human edit); a fix for a new ``path`` is
    appended. Deliberately matches on exact ``path``, not on ``category``/
    domain -- a finding-domain like "security" covers many unrelated skills
    (containerfile, rbac, network-policy, resource-limits, ...), so
    replacing everything sharing a domain would silently drop an already-
    correct sibling skill's output just because one other skill in the same
    domain needed a fix.
    """
    by_path = {f["path"]: i for i, f in enumerate(current_files)}
    merged = list(current_files)
    for nf in new_files:
        if nf["path"] in by_path:
            merged[by_path[nf["path"]]] = nf
        else:
            merged.append(nf)
    return merged


async def _dry_run_check(
    files: list[dict], *, app_name: str, namespace: str, report: AssessmentReport | None,
    store: object, assessment_id: str, actor: str,
) -> tuple[list[str], set[str]]:
    """Runs ``route_and_deliver(dry_run=True)`` and returns
    (error-messages, placeholder-blocked-paths). Never persists a
    ``deliveries`` row differently than any other dry run already does --
    this is the exact same call ``deliver()``'s manual "Dry Run" button
    makes today, just invoked automatically."""
    from agentit.portal.delivery import route_and_deliver

    result = await route_and_deliver(
        files, app_name=app_name, namespace=namespace, report=report,
        store=store, assessment_id=assessment_id, actor=actor, dry_run=True,
    )
    errors = [
        f"{cat}: {o['error']}" for cat, o in result["outcomes"].items()
        if isinstance(o, dict) and o.get("error")
    ]
    return errors, set(result.get("placeholder_blocked") or [])


def _check_properties(files: list[dict]) -> list:
    """Wraps ``property_verifier.verify_all_properties()`` over the plain
    dict-shaped files this module works with everywhere else."""
    from agentit.agents.base import GeneratedFile
    from agentit.property_verifier import verify_all_properties

    generated = [
        GeneratedFile(path=f["path"], content=f.get("content", ""), description=f.get("description", f["path"]))
        for f in files
    ]
    return verify_all_properties(generated)


async def validate_and_fix_manifests(
    files: list[dict],
    *,
    app_name: str,
    namespace: str,
    report: AssessmentReport | None,
    store: object,
    assessment_id: str,
    actor: str,
    max_iterations: int = MAX_VALIDATION_ITERATIONS,
    job_id: str | None = None,
) -> dict:
    """The iterative validate -> fix -> re-validate loop.

    Each iteration: (1) run the same structural dry-run check the manual
    "Dry Run" button runs (blocked/placeholder/no-GitOps-registration
    errors); (2) run property_verifier's structural correctness check; (3)
    for every failure this module knows how to retry -- a failed property
    mapped to a real finding category, or a placeholder-blocked file's own
    category -- dispatch a fresh regeneration via ``RemediationDispatcher``
    (the exact machinery a human's "Fix" button already uses) and merge the
    result in; (4) loop. Stops as soon as a pass comes back fully clean, OR
    as soon as an iteration produces zero fixes (retrying an identical
    failure would just repeat it), OR after ``max_iterations`` -- whichever
    comes first. Never silently declares success: the returned ``clean``
    flag is the honest, sole source of truth for whether this converged.

    Returns {"files": list[dict], "clean": bool, "iterations": list[dict]}.
    """
    current_files = list(files)
    iterations: list[dict] = []

    from agentit.remediation.dispatcher import RemediationDispatcher
    dispatcher = RemediationDispatcher(store)

    for i in range(1, max_iterations + 1):
        if job_id:
            await store.update_remediation_job(
                job_id, "validating", f"Validating generated manifests (attempt {i} of {max_iterations})...",
            )

        dry_errors, placeholder_blocked = await _dry_run_check(
            current_files, app_name=app_name, namespace=namespace, report=report,
            store=store, assessment_id=assessment_id, actor=actor,
        )
        # property_verifier.verify_all_properties() is a blanket check of
        # all four properties regardless of what this assessment actually
        # flagged -- scope every failure down to the ones tied to a REAL
        # finding for this assessment before deciding either (a) whether
        # this counts as "clean", or (b) whether to attempt a fix. Without
        # this, an app whose only real finding is (say) missing RBAC could
        # never converge at all: NetworkPolicy/HPA/ServiceMonitor would
        # keep "failing" forever, for something this onboarding never
        # claimed to fix in the first place and this loop deliberately
        # never attempts (see _assessment_has_finding_category()).
        relevant_failed = [
            r for r in _check_properties(current_files)
            if not r.passed and _assessment_has_finding_category(
                report, _PROPERTY_TO_FIX_CATEGORY.get(r.property_name, ""),
            )
        ]

        iteration_record = {
            "iteration": i,
            "dry_run_errors": dry_errors,
            "failed_properties": [r.property_name for r in relevant_failed],
            "fixed_categories": [],
        }

        if not dry_errors and not relevant_failed:
            iterations.append(iteration_record)
            return {"files": current_files, "clean": True, "iterations": iterations}

        fixed_categories: list[str] = []

        for result in relevant_failed:
            category = _PROPERTY_TO_FIX_CATEGORY[result.property_name]
            fix_result = await dispatcher.dispatch(assessment_id, category, app_name)
            if fix_result.get("files"):
                current_files = _merge_fix_files(current_files, fix_result["files"])
                fixed_categories.append(result.property_name)

        if placeholder_blocked:
            retry_categories = {f["category"] for f in current_files if f["path"] in placeholder_blocked}
            for category in retry_categories:
                fix_result = await dispatcher.dispatch(assessment_id, category, app_name)
                if fix_result.get("files"):
                    current_files = _merge_fix_files(current_files, fix_result["files"])
                    fixed_categories.append(f"placeholder:{category}")

        iteration_record["fixed_categories"] = fixed_categories
        iterations.append(iteration_record)

        if not fixed_categories:
            # Nothing this loop knows how to act on changed this round --
            # another identical attempt would just reproduce the same
            # failure, so stop now rather than burning the remaining
            # iterations for no reason.
            break

    return {
        "files": current_files,
        "clean": False,
        "iterations": iterations,
        "remaining_issues": iterations[-1]["dry_run_errors"] + iterations[-1]["failed_properties"],
    }


async def review_final_manifests(llm_client: object | None, files: list[dict], report: AssessmentReport) -> dict | None:
    """Wraps ``LLMClient.review_final_manifests()`` -- the one-time final
    quality/completeness pass over the whole validated batch, run once the
    validate/fix loop above has converged. Returns ``None`` (not a failure)
    when no LLM client is configured -- an unreviewed PR is exactly today's
    baseline behavior (every manual Deliver click already skips any LLM
    opinion), not a regression this module introduces."""
    if llm_client is None:
        return None
    app_summary = f"{report.repo_name} ({', '.join(l.name for l in report.stack.languages[:3]) or 'unknown stack'})"
    return llm_client.review_final_manifests(files, app_summary)


async def notify_pr_ready(
    store: object, app_name: str, assessment_id: str, delivery: dict, review: dict | None,
) -> list[str]:
    """The "you have PR(s) waiting for your approval" signal for an
    automatically-completed delivery -- sourced directly from
    ``route_and_deliver()``'s own returned ``outcomes`` (each one already
    carries its own ``pr_url`` the moment a real, non-dry-run commit/PR
    call succeeds), never from a ``gates`` query: the ``gates`` table is
    being removed system-wide by a separate effort, and Ledger's "waiting
    for your approval" section is being fixed to read the same real PR
    state this reads, so this notification keeps working unchanged once
    that lands. Publishes the existing event-bus/log_event mechanism this
    app already uses for every other "something happened" signal (Fleet's
    badge, Assessment Detail's Timeline, the Ledger feed) -- no new
    notification channel invented. Returns the list of PR URLs opened.
    """
    from agentit.portal.helpers import publish_event

    pr_urls = [
        o["pr_url"] for o in delivery["outcomes"].values()
        if isinstance(o, dict) and o.get("pr_url")
    ]
    if not pr_urls:
        return []

    summary = (
        f"Automatic validation complete -- {len(pr_urls)} pull request(s) ready for your "
        f"approval: {', '.join(pr_urls)}"
    )
    if review is not None and not review.get("approved", True):
        summary += f" (LLM final review flagged concerns: {review.get('reason', '')})"

    publish_event(
        "onboarding-pr-ready", app_name, summary,
        details={"pr_urls": pr_urls, "assessment_id": assessment_id},
        correlation_id=assessment_id, agent_id="auto-delivery",
    )
    try:
        await store.log_event(
            "auto-delivery", "onboarding-pr-ready", app_name,
            "warning" if (review is not None and not review.get("approved", True)) else "info",
            summary, correlation_id=assessment_id,
        )
    except Exception:
        logger.warning("Failed to log onboarding-pr-ready event for %s", app_name, exc_info=True)
    return pr_urls


async def auto_validate_and_deliver(
    *,
    store: object,
    report: AssessmentReport,
    app_name: str,
    namespace: str,
    assessment_id: str,
    actor: str,
    files: list[dict],
    orchestration: dict,
    target_findings: list[tuple[str, str]] | None = None,
    job_id: str | None = None,
    score_delta_claimed: float | None = None,
) -> dict:
    """The top-level pipeline: validate/fix loop -> finding gate -> cluster
    PRs -> final LLM review -> real deliver -> notify.

    Quality bar (docs/plan-quality-helpful-prs.md Phases A–D/F): refuse PRs
    with no open findings / score delta; drop files not tied to those
    findings; open ≤ one PR per finding cluster after per-cluster
    validation; PR bodies explain finding → change → expected outcome.

    Returns a dict describing the outcome:
    - ``{"status": "needs_attention", "reason": str, "iterations": [...]}``
      when the validate/fix loop could not converge within
      ``MAX_VALIDATION_ITERATIONS`` -- manifests are still saved (whatever
      partial progress the loop made), nothing is delivered, and this is
      surfaced honestly rather than faking success.
    - ``{"status": "delivered", "delivery": {...}, "pr_urls": [...],
      "review": {...} | None}`` once a real PR (or more than one) has been
      opened.
    - ``{"status": "unchanged", "delivery": {...}, "review": {...} | None}``
      when validation converged and delivery produced no error, but also no
      PR, because every category's generated content already matched what's
      deployed (github_pr.py's content-unchanged dedup) -- a benign no-op,
      not a failure.
    - ``{"status": "delivery_failed", "reason": str}`` when validation
      converged but the real ``route_and_deliver()`` call itself produced
      no successful outcome at all (every category errored) -- also
      surfaced honestly, never silently swallowed.
    - ``{"status": "needs_attention", "reason": str}`` also when the
      self-managed chart delivery gate refuses (non-Helm / collision /
      forbidden kinds), or Phase A/C quality gates refuse — no PR, never a
      green success path. See ``delivery.validate_self_managed_chart_delivery``
      and ``portal.quality_prs``.
    """
    from agentit.portal.delivery import route_and_deliver
    from agentit.portal.helpers import get_llm_client
    from agentit.portal.quality_prs import (
        build_helpful_pr_body,
        cluster_validation_ok,
        filter_files_to_open_findings,
        finding_gate_allows_pr,
        finding_gate_refuse_reason,
        partition_by_finding_cluster,
        resolve_target_findings,
    )

    validation = await validate_and_fix_manifests(
        files, app_name=app_name, namespace=namespace, report=report,
        store=store, assessment_id=assessment_id, actor=actor, job_id=job_id,
    )
    final_files = validation["files"]
    await store.save_onboarding(
        assessment_id, final_files,
        orchestration={**orchestration, "auto_validation": {
            "iterations": validation["iterations"], "converged": validation["clean"],
        }},
    )

    if validation["clean"]:
        # Mirrors the exact shape the manual "Dry Run" button's own
        # save_apply_results() call already persists (routes/
        # assessments.py::deliver()) -- so Onboard Results' existing
        # dry_run_done gating reflects reality after THIS loop converges
        # too. Never marks a real delivery here -- only that validation
        # passed -- the PR cards/pr_tracking.py remain the sole source of
        # truth for whether anything was actually delivered.
        try:
            await store.save_apply_results(
                assessment_id,
                {
                    "applied": [], "skipped": [], "errors": [],
                    "repo_files": [
                        {"path": f["path"], "purpose": "validated by automatic validation loop"}
                        for f in final_files
                    ],
                },
                namespace, dry_run=True,
            )
        except Exception:
            logger.warning("Failed to persist apply_results for %s after validation converged", app_name, exc_info=True)

    if not validation["clean"]:
        reason = (
            "Automatic validation could not converge after "
            f"{MAX_VALIDATION_ITERATIONS} attempt(s): "
            + "; ".join(validation.get("remaining_issues") or ["unknown issue"])
        )
        logger.warning("Auto-validation did not converge for %s: %s", app_name, reason)
        try:
            await store.log_event(
                "auto-delivery", "auto-validation-needs-attention", app_name, "warning",
                f"{reason} -- manifests were saved; review on Ledger / Scan Results.",
                correlation_id=assessment_id,
            )
        except Exception:
            logger.warning("Failed to log auto-validation-needs-attention event for %s", app_name, exc_info=True)
        return {"status": "needs_attention", "reason": reason, "iterations": validation["iterations"]}

    # Phase A: finding / score-delta gate — no catalog dumps with empty need.
    resolved_findings = resolve_target_findings(report, target_findings)
    if not finding_gate_allows_pr(resolved_findings, score_delta_claimed=score_delta_claimed):
        reason = finding_gate_refuse_reason(resolved_findings)
        logger.warning("Auto-delivery finding gate refused for %s: %s", app_name, reason)
        try:
            await store.log_event(
                "auto-delivery", "auto-delivery-finding-gate", app_name, "warning",
                reason, correlation_id=assessment_id,
            )
        except Exception:
            logger.warning("Failed to log finding-gate event for %s", app_name, exc_info=True)
        return {"status": "needs_attention", "reason": reason}

    kept_files, drop_reasons = filter_files_to_open_findings(final_files, resolved_findings)
    if not kept_files:
        reason = (
            "No generated files map to open findings — refusing PR. "
            + ("; ".join(drop_reasons[:5]) if drop_reasons else "empty after finding filter")
        )
        logger.warning("Auto-delivery finding filter emptied batch for %s: %s", app_name, reason)
        try:
            await store.log_event(
                "auto-delivery", "auto-delivery-finding-gate", app_name, "warning",
                reason, correlation_id=assessment_id,
            )
        except Exception:
            logger.warning("Failed to log finding-filter event for %s", app_name, exc_info=True)
        return {"status": "needs_attention", "reason": reason, "drop_reasons": drop_reasons}

    if job_id:
        await store.update_remediation_job(job_id, "reviewing", "Running final quality review...")
    llm_client = get_llm_client()
    review = await review_final_manifests(llm_client, kept_files, report)
    if review is not None and not review.get("approved", True):
        logger.info("Final LLM review flagged concerns for %s: %s", app_name, review.get("reason"))

    # Phase B: one PR per finding cluster (fleet + self-managed parity — Phase F).
    clusters = partition_by_finding_cluster(kept_files, resolved_findings)
    if job_id:
        await store.update_remediation_job(
            job_id, "delivering",
            f"Creating pull request(s) for {len(clusters)} finding cluster(s)...",
        )

    score_dims = [s.dimension for s in report.scores if s.findings]
    all_pr_urls: list[str] = []
    cluster_refusals: list[str] = []
    last_delivery: dict | None = None
    delivered_any = False
    unchanged_any = False

    for cluster in clusters:
        # Phase C: per-cluster validation before open.
        dry_errors, _ = await _dry_run_check(
            cluster.files, app_name=app_name, namespace=namespace, report=report,
            store=store, assessment_id=assessment_id, actor=actor,
        )
        relevant_failed = [
            r for r in _check_properties(cluster.files)
            if not r.passed and _assessment_has_finding_category(
                report, _PROPERTY_TO_FIX_CATEGORY.get(r.property_name, ""),
            )
        ]
        ok, refuse_reason = cluster_validation_ok(
            dry_run_errors=dry_errors,
            failed_properties=[r.property_name for r in relevant_failed],
        )
        if not ok:
            cluster_refusals.append(f"{cluster.key}: {refuse_reason}")
            logger.warning(
                "Auto-delivery cluster %s for %s failed validation: %s",
                cluster.key, app_name, refuse_reason,
            )
            continue

        pr_context = {
            "body": build_helpful_pr_body(
                title_line=f"AgentIT Scan: {cluster.key} for {app_name}",
                target_findings=cluster.target_findings,
                files=cluster.files,
                validation_summary=(
                    "SSA dry-run (concrete YAML), property checks for targeted "
                    "findings, and self-managed chart gate (#119) passed for this cluster."
                ),
                drop_reasons=drop_reasons,
                score_dimensions=score_dims,
            ),
            "branch_suffix": cluster.branch_suffix,
            "cluster_key": cluster.key,
        }
        delivery = await route_and_deliver(
            cluster.files, app_name=app_name, namespace=namespace, report=report,
            store=store, assessment_id=assessment_id, actor=actor, dry_run=False,
            target_findings=cluster.target_findings,
            pr_context=pr_context,
        )
        last_delivery = delivery
        if review is not None and not review.get("approved", True):
            try:
                await store.update_delivery(delivery["delivery_id"], details={"llm_review": review})
            except Exception:
                logger.warning(
                    "Failed to attach llm_review to delivery %s",
                    delivery.get("delivery_id"), exc_info=True,
                )

        cluster_prs = [
            o["pr_url"] for o in delivery["outcomes"].values()
            if isinstance(o, dict) and o.get("pr_url")
        ]
        if cluster_prs:
            delivered_any = True
            all_pr_urls.extend(cluster_prs)
        else:
            delivery_errors = [
                o["error"] for o in delivery["outcomes"].values()
                if isinstance(o, dict) and o.get("error")
            ]
            if delivery_errors:
                gate_refused = any(
                    isinstance(o, dict) and o.get("gate_refused")
                    for o in delivery["outcomes"].values()
                )
                reason = "; ".join(delivery_errors)
                cluster_refusals.append(f"{cluster.key}: {reason}")
                if gate_refused:
                    try:
                        await store.log_event(
                            "auto-delivery", "auto-delivery-gate-refused", app_name, "warning",
                            f"Cluster {cluster.key} refused: {reason}",
                            correlation_id=assessment_id,
                        )
                    except Exception:
                        logger.warning("Failed to log gate-refused for %s", app_name, exc_info=True)
            else:
                unchanged_any = True

    if all_pr_urls and last_delivery is not None:
        # Reuse notify with a synthetic outcomes map so Ledger gets one signal.
        notify_delivery = {
            "delivery_id": last_delivery.get("delivery_id"),
            "outcomes": {f"cluster_{i}": {"pr_url": u} for i, u in enumerate(all_pr_urls)},
        }
        await notify_pr_ready(store, app_name, assessment_id, notify_delivery, review)
        return {
            "status": "delivered",
            "delivery": last_delivery,
            "pr_urls": all_pr_urls,
            "review": review,
            "clusters": len(clusters),
            "drop_reasons": drop_reasons,
        }

    if cluster_refusals and not unchanged_any:
        reason = "; ".join(cluster_refusals)
        gate_refused = any(
            "gate" in r.lower() or "Helm-shaped" in r or "filter dropped" in r
            for r in cluster_refusals
        )
        logger.warning("Auto-delivery produced no PR for %s: %s", app_name, reason)
        try:
            await store.log_event(
                "auto-delivery",
                "auto-delivery-gate-refused" if gate_refused else "auto-delivery-failed",
                app_name, "warning",
                f"Automatic delivery did not open a pull request: {reason}",
                correlation_id=assessment_id,
            )
        except Exception:
            logger.warning("Failed to log auto-delivery outcome event for %s", app_name, exc_info=True)
        if gate_refused or any("SSA" in r or "Property check" in r or "finding" in r for r in cluster_refusals):
            return {"status": "needs_attention", "reason": reason, "drop_reasons": drop_reasons}
        return {"status": "delivery_failed", "reason": reason, "drop_reasons": drop_reasons}

    # No pull request AND no hard error: content-unchanged dedup across clusters.
    logger.info("Auto-delivery for %s: nothing to deliver -- manifests already match what's deployed", app_name)
    try:
        await store.log_event(
            "auto-delivery", "auto-delivery-unchanged", app_name, "info",
            "Automatic validation complete -- generated manifests already match what's deployed; "
            "no new pull request needed.",
            correlation_id=assessment_id,
        )
    except Exception:
        logger.warning("Failed to log auto-delivery-unchanged event for %s", app_name, exc_info=True)
    return {
        "status": "unchanged",
        "delivery": last_delivery or {},
        "review": review,
        "drop_reasons": drop_reasons,
    }

