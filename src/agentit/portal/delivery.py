"""The unified apply flow's router: classify generated files into
docs/unified-apply-flow.md's taxonomy, decide whether an app is
GitOps-registered, and route each classified group to exactly one delivery
mechanism -- closing the gap where "Apply to Cluster", "Create PR",
gate-approve, ``AutoMode``, and ``DriftDetector`` could each independently
decide "this change reaches a cluster/repo now" with no shared decision, no
shared verification tail, and no awareness of each other.

See docs/unified-apply-flow.md for the full design this module implements.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from agentit import kube
from agentit.audit import audit_log
from agentit.models import AssessmentReport
from agentit.portal.cluster_apply import _OPERATOR_NAMESPACES, _parse_manifest
from agentit.skill_engine import record_skill_outcomes

logger = logging.getLogger(__name__)

# ── Taxonomy categories (docs/unified-apply-flow.md section (D)) ──────────
CATEGORY_CLUSTER_CONFIG = "cluster_config"
CATEGORY_CICD_SHARED_NAMESPACE = "cicd_shared_namespace"
CATEGORY_SOURCE_PATCH = "source_patch"
CATEGORY_NARRATIVE_REPORT = "narrative_report"
CATEGORY_MANIFEST_AT_REST = "manifest_at_rest"
CATEGORY_SECRET_BLOCKED = "secret_blocked"

# How many confirmed "still present after the fix" delivery attempts, for
# the same (app, finding-category), before this stops auto-retrying and
# escalates to a human instead. 3 matches this codebase's existing
# precedent for the identical "how many failed attempts before backing off"
# shape: webhooks.py's `get_rejection_count(...) >= 3` (skip an
# auto-fixable category that's been rejected 3+ times) and
# skill_learner.py's `improvement_cooldown_attempts` default (back off a
# flagged skill after 3 failed improvement attempts, 0724871).
FINDING_ESCALATION_THRESHOLD = 3

# Mechanisms a category can be routed to. MECHANISM_DIRECT_APPLY is no
# longer a selectable live outcome (Direct Apply has been removed as a
# concept entirely -- see resolve_cluster_config_mechanism()) -- the
# constant/string survives only so historical `deliveries` rows already
# persisted with this mechanism (from before this directive landed) still
# render honest text instead of a blank/`KeyError` lookup.
MECHANISM_DIRECT_APPLY = "direct-apply"
MECHANISM_INFRA_REPO_COMMIT = "infra-repo-commit"
# Retired (2026-07-18, alongside the direct-apply cluster-admin-review gate
# type) -- kept only so historical `deliveries` rows already persisted with
# this mechanism string still render honest text (see MECHANISM_DIRECT_
# APPLY's identical precedent above). CATEGORY_CICD_SHARED_NAMESPACE now
# resolves to MECHANISM_INFRA_REPO_COMMIT, same as CATEGORY_CLUSTER_CONFIG.
MECHANISM_CLUSTER_ADMIN_REVIEW_GATE = "cluster-admin-review-gate"
MECHANISM_SOURCE_REPO_PR = "source-repo-pr"
MECHANISM_APP_REPO_PR = "app-repo-pr"
MECHANISM_NONE = "none"

# The distinct GitOps-infra-repo subfolder (apps/{app}/{this}/*, in place of
# a file's own generation-source `category`) and Git branch (in place of the
# app-config category's shared `agentit/{app}` branch) CI/CD-shared-namespace
# manifests commit to -- see _deliver_via_gitops_pr() below. Two reasons this
# must never share cluster-config's own branch/folder: (1) a human reviewer
# needs an unambiguous visual signal ("this PR/folder touches a namespace
# every app on the cluster shares, not just this app's own") per the
# 2026-07-18 cluster-admin-review-removal decision; (2) commit_to_infra_
# repo() force-pushes whatever branch name it's given, rebased fresh off the
# infra repo's default branch every call -- two categories sharing one
# branch name within the same route_and_deliver() call would silently
# clobber each other's commit instead of both landing.
_CICD_SHARED_NAMESPACE_PATH_PREFIX = "cicd-shared-namespace"

_NARRATIVE_REPORT_FILENAMES = frozenset({"dependency-report.md", "cost-report.md"})

# Unsubstituted image/token placeholders that must never reach a real
# cluster or GitOps PR (agents/base.py defaults CronJob images to this).
_UNRESOLVED_PLACEHOLDERS = ("REPLACE_WITH_AGENTIT_IMAGE",)


def has_unresolved_placeholders(content: str | None) -> bool:
    """True when generated file content still contains bootstrap placeholders."""
    text = content or ""
    return any(marker in text for marker in _UNRESOLVED_PLACEHOLDERS)


# Human-facing mechanism descriptions surfaced at the confirmation step a
# human must actively acknowledge before a real delivery fires (per the
# 2026-07-14 customer-review addendum to docs/unified-apply-flow.md: a
# dynamically-relabeled button alone is "less on-screen signal than two
# buttons", so the *reason* a mechanism was chosen must be spelled out at
# the point of no return, not only on an earlier, skippable dry-run screen).
MECHANISM_DESCRIPTIONS: dict[str, str] = {
    MECHANISM_DIRECT_APPLY: "Apply these manifests directly to the cluster -- no GitOps registration was found for this app.",
    MECHANISM_INFRA_REPO_COMMIT: "Commit to the GitOps infra repo and open a PR -- this app is GitOps-registered via a live Argo CD Application. A human must still merge the PR; AgentIT will never auto-merge.",
    # Retired (2026-07-18) -- see MECHANISM_CLUSTER_ADMIN_REVIEW_GATE's own
    # comment above; kept only for historical-record rendering.
    MECHANISM_CLUSTER_ADMIN_REVIEW_GATE: "Held for cluster-admin review -- these manifests targeted a shared operator namespace this service account could not apply to without elevated RBAC. (Retired: this category now delivers via a GitOps PR instead.)",
    MECHANISM_SOURCE_REPO_PR: "Open a PR against this app's code repo with a real patch to the named file(s).",
    MECHANISM_APP_REPO_PR: "Open an informational PR against this app's code repo with these files under `.agentit/`.",
    MECHANISM_NONE: "Nothing to deliver.",
}

# Which repo (docs/unified-apply-flow.md's two distinct repos-in-play: the
# app's own code repo vs. its GitOps infra repo) a commit/PR mechanism's URL
# actually targets -- the single source of truth every PR-reference surface
# (onboard_results.html's Delivery History + flash alerts, the Ledger's
# delivery cards, confirmation_text() above) should trace back to instead of
# each independently guessing from the mechanism name. Traces the real
# mechanism-to-repo mapping route_and_deliver() already encodes:
# MECHANISM_INFRA_REPO_COMMIT commits to report.infra_repo_url (the GitOps
# repo); MECHANISM_SOURCE_REPO_PR/MECHANISM_APP_REPO_PR both open a PR
# against report.repo_url (the app's own code repo) -- see
# deliver_with_verification() above. Direct-apply/cluster-admin-review-
# gate/none never touch a repo at all, so they map to "".
_MECHANISM_REPO_KIND: dict[str, str] = {
    MECHANISM_INFRA_REPO_COMMIT: "gitops",
    MECHANISM_SOURCE_REPO_PR: "code",
    MECHANISM_APP_REPO_PR: "code",
}


def repo_kind_for_mechanism(mechanism: str) -> str:
    """``"code"``, ``"gitops"``, or ``""`` (no repo target) for a delivery
    mechanism -- for labeling a PR/commit link with which of the app's two
    repos it actually targets. See ``_MECHANISM_REPO_KIND`` above."""
    return _MECHANISM_REPO_KIND.get(mechanism, "")


def resolve_cluster_config_mechanism(infra_repo_url: str | None) -> str:
    """The cluster/app-config category's delivery mechanism, shared by every
    caller that predicts or acts on it (``route_and_deliver()`` and the
    dry-run preview on Onboard Results) so they can never disagree about
    what a given ``infra_repo_url`` resolves to.

    Direct Apply has been removed as a concept entirely (product directive:
    all apps must use GitOps; GitHub-PR-merge is the only sanctioned gate) --
    this can no longer select ``MECHANISM_DIRECT_APPLY`` as a live outcome,
    for any caller, ever. Knowing an infra repo URL is the only thing that
    matters now: whether a live Argo CD ``Application`` already exists
    (``registered``, formerly a parameter here) is irrelevant to *whether* to
    commit -- only *whether this is the first commit that bootstraps
    ``apps/{app}/`` for Argo's ``ApplicationSet`` to discover* (see
    docs/onboarding-loop-vision-gap-analysis.md §1), which
    ``deliver_with_verification()``'s ``MECHANISM_INFRA_REPO_COMMIT`` branch
    already handles identically either way.

    GitOps registration is mandatory for every new assessment
    (``routes/assessments.py``'s ``_resolve_mandatory_infra_repo_url()``
    hard-stops Assess otherwise -- see the README's "GitOps registration is
    now mandatory" entry), so ``infra_repo_url`` should always be known in
    practice. The ``None`` case below is only reachable for an assessment
    saved before that directive landed (no infra repo was ever recorded) --
    this refuses to guess rather than falling back to a direct apply; a
    human must register this app for GitOps (e.g. via "Register for GitOps")
    before it can be delivered at all.
    """
    if infra_repo_url is not None:
        return MECHANISM_INFRA_REPO_COMMIT
    return MECHANISM_NONE


def confirmation_text(mechanism: str, *, infra_repo_url: str | None = None) -> str:
    """The exact statement a human must see -- and actively acknowledge --
    immediately before ``route_and_deliver()`` actually fires for the
    cluster/app-config category, via the portal's unified "Deliver" action.
    Reused verbatim by both the dry-run preview *and* the point-of-no-return
    confirmation dialog, so the two can never say different things about
    the same decision.

    For the direct-apply path, this names the *target cluster* (API server
    host, or in-cluster) alongside the *action* -- ``kube.get_client()``
    resolves silently to whatever cluster the ambient kubeconfig/in-cluster
    config happens to point at, with no prior visibility to the human
    approving the apply. Naming it here (still synchronous --
    ``kube.get_current_cluster_identity()`` never makes a live call to the
    API server, just reads back the already-resolved client config) is
    real safety signal a human needs before a destructive action, not
    cosmetic.

    ``MECHANISM_DIRECT_APPLY`` handling below is kept for two reasons even
    though ``resolve_cluster_config_mechanism()`` can never select it as a
    live outcome anymore: historical ``deliveries`` rows already persisted
    with this mechanism string still need honest, renderable text if ever
    re-derived rather than a `KeyError`/blank string, and this function has
    no way to distinguish "asked to describe a mechanism that
    can no longer be chosen" from "asked to describe a legacy record."
    """
    base = MECHANISM_DESCRIPTIONS.get(mechanism, mechanism)
    if mechanism == MECHANISM_INFRA_REPO_COMMIT and infra_repo_url:
        return f"AgentIT will: commit to `{infra_repo_url}` and open a PR -- this app is GitOps-registered via a live Argo CD Application. A human must merge; AgentIT will never auto-merge."
    if mechanism == MECHANISM_DIRECT_APPLY:
        cluster_label = kube.get_current_cluster_identity()["label"]
        return f"AgentIT will: apply these manifests directly to the cluster ({cluster_label}) -- no GitOps registration was found for this app."
    return f"AgentIT will: {base}"


def classify_file(entry: dict) -> str:
    """Classify one generated file into docs/unified-apply-flow.md's
    taxonomy. Reuses ``cluster_apply.py``'s existing YAML-parsing helper
    (kept, not replaced, per the design doc) -- this only adds the
    category-level decision on top of it.
    """
    category = entry.get("category", "")
    path = entry.get("path", "")
    suffix = Path(path).suffix.lower()

    # CodeChangeAgent output -- explicitly tagged by category rather than
    # guessed from file extension, per the design doc. Its own summary file
    # is documentation about the changes, not a change itself.
    if category == "codechange":
        if path == "code-changes-summary.md":
            return CATEGORY_NARRATIVE_REPORT
        return CATEGORY_SOURCE_PATCH

    # DependencyAgent/CostOptimizationAgent's narrative reports -- real
    # computed data, not a template, and never a delivery candidate (see
    # taxonomy row "Narrative reports").
    if category in ("dependency", "cost") and path in _NARRATIVE_REPORT_FILENAMES:
        return CATEGORY_NARRATIVE_REPORT

    if suffix not in (".yaml", ".yml"):
        return CATEGORY_MANIFEST_AT_REST

    docs = _parse_manifest(entry.get("content", ""))
    if not docs:
        return CATEGORY_MANIFEST_AT_REST

    # Secrets/credential-adjacent changes -- named in the design doc as a
    # permanent deny-rule, not a routing gap: never deliver via any
    # mechanism, ever.
    for doc in docs:
        if (doc.get("kind") or "") == "Secret":
            return CATEGORY_SECRET_BLOCKED

    for doc in docs:
        meta = doc.get("metadata") or {}
        if meta.get("namespace", "") in _OPERATOR_NAMESPACES:
            return CATEGORY_CICD_SHARED_NAMESPACE

    return CATEGORY_CLUSTER_CONFIG


def preview_delivery_groups(
    files: list[dict], *, infra_repo_url: str | None,
) -> dict[str, dict]:
    """Read-only preview of how ``route_and_deliver()`` would group and
    route ``files`` right now -- no commits, no PRs, no store writes. Runs
    the exact same classify -> exclude-blocked/narrative -> strip-
    placeholders -> mechanism-resolution sequence ``route_and_deliver()``
    itself performs first, extracted here so a caller (Onboard Results'
    pre-delivery "AgentIT will open N PR(s)" preview) shows the same real
    taxonomy/mechanism decision a real Deliver click will make, instead of
    a second, drifting approximation of it.

    Returns ``{category: {"files": [...], "mechanism": ..., "repo_kind":
    ..., "confirmation": ...}}`` for every deliverable category
    (``CATEGORY_CLUSTER_CONFIG``/``CATEGORY_CICD_SHARED_NAMESPACE``/
    ``CATEGORY_SOURCE_PATCH``/``CATEGORY_MANIFEST_AT_REST``) that still has
    at least one file after that sequence.
    ``CATEGORY_SECRET_BLOCKED``/``CATEGORY_NARRATIVE_REPORT`` are never
    included -- neither is ever routed to any delivery mechanism (see
    ``route_and_deliver()``'s own handling of both).
    """
    groups: dict[str, list[dict]] = {}
    for f in files:
        groups.setdefault(classify_file(f), []).append(f)
    groups.pop(CATEGORY_SECRET_BLOCKED, None)
    groups.pop(CATEGORY_NARRATIVE_REPORT, None)

    result: dict[str, dict] = {}
    for category, group_files in groups.items():
        keep = [f for f in group_files if not has_unresolved_placeholders(f.get("content"))]
        if not keep:
            continue
        if category in (CATEGORY_CLUSTER_CONFIG, CATEGORY_CICD_SHARED_NAMESPACE):
            mechanism = resolve_cluster_config_mechanism(infra_repo_url)
        elif category == CATEGORY_SOURCE_PATCH:
            mechanism = MECHANISM_SOURCE_REPO_PR
        elif category == CATEGORY_MANIFEST_AT_REST:
            mechanism = MECHANISM_APP_REPO_PR
        else:
            continue
        result[category] = {
            "files": keep,
            "mechanism": mechanism,
            "repo_kind": repo_kind_for_mechanism(mechanism),
            "confirmation": confirmation_text(mechanism, infra_repo_url=infra_repo_url),
        }
    return result


def _sanitize_app_name(app_name: str) -> str:
    return app_name.lower().replace("_", "-").replace(".", "-")


def gitops_application_name(app_name: str) -> str:
    """The Argo CD ``Application`` name a GitOps-registered app's manifests
    sync through -- ``github_pr.ensure_applicationset()``'s ApplicationSet
    template names every generated ``Application`` ``managed-{basename}``
    (``github_pr.py``'s ``spec.generators[0].git.template.metadata.name``).
    Single source of truth for that naming convention so
    ``is_gitops_registered()`` (one live-lookup-per-app) and any bulk
    Fleet-wide enrichment that already lists every ``Application`` in
    ``openshift-gitops`` (``routes/fleet.py``) can check the same name
    without duplicating -- and risking drifting -- the convention.
    """
    return f"managed-{_sanitize_app_name(app_name)}"


def application_source_repo_url(application: dict) -> str | None:
    """The code repo an Argo CD ``Application`` dict deploys from --
    ``spec.source.repoURL``, falling back to ``spec.sources[0].repoURL`` for
    multi-source Applications. Single place both ``is_gitops_registered()``
    and the Fleet-wide enrichment (``routes/fleet.py``) read this from, so
    the two stay in sync.
    """
    spec = application.get("spec") or {}
    source = spec.get("source") or {}
    if not source:
        sources = spec.get("sources") or []
        source = sources[0] if sources else {}
    return source.get("repoURL")


def is_self_managed_application(candidate_repo_url: str | None, app_repo_url: str | None) -> bool:
    """Whether a literal-named Argo CD Application's own source repo
    (``candidate_repo_url``, from ``application_source_repo_url()``)
    actually matches ``app_repo_url`` (the fleet app's own code repo).

    Apps that register themselves into their own fleet (e.g. AgentIT via
    ``register-self-in-fleet``) are deliberately excluded from the shared
    ``apps/*``-directory ApplicationSet (``github_pr.ensure_applicationset()``
    excludes ``apps/agentit`` specifically, to avoid a circular/duplicate
    Application) and instead run under a hand-crafted Application named for
    the app itself (``argocd/application.yaml``'s ``agentit``, not
    ``managed-agentit``). Comparing source repo URLs -- not just presence of
    an Application with a matching name -- is what lets that literal-named
    Application count as this app's own GitOps registration without also
    matching an unrelated Application that just happens to share the name
    (e.g. a hand-created demo Application pointed at a placeholder repo).
    """
    if not candidate_repo_url or not app_repo_url:
        return False
    from agentit.portal.store import normalize_repo_url
    return normalize_repo_url(candidate_repo_url) == normalize_repo_url(app_repo_url)


async def is_gitops_registered(
    app_name: str, report: AssessmentReport | None,
) -> tuple[bool, str | None]:
    """Whether ``app_name`` is GitOps-registered -- a real query ("does a
    live Argo CD ``Application`` exist targeting this namespace") rather
    than "was an infra repo URL set once", per the design doc's plumbing-gap
    fix. Falls back to ``report.infra_repo_url is not None`` only when the
    cluster call itself fails (unreachable/offline cluster, e.g. tests) --
    a successful call that simply finds no ``Application`` is NOT registered
    regardless of ``infra_repo_url``.

    Also counts as registered when a literal-named Application (rather than
    the ``managed-{app}`` one) exists and actually sources from this app's
    own repo -- see ``is_self_managed_application()`` for why (the
    self-referential ``register-self-in-fleet`` case).
    """
    infra_repo_url = report.infra_repo_url if report is not None else None
    try:
        app = await asyncio.to_thread(
            kube.get_custom_resource,
            "argoproj.io", "v1alpha1", "applications", gitops_application_name(app_name),
            namespace="openshift-gitops",
        )
        if app is None and report is not None and report.repo_url:
            literal = await asyncio.to_thread(
                kube.get_custom_resource,
                "argoproj.io", "v1alpha1", "applications", _sanitize_app_name(app_name),
                namespace="openshift-gitops",
            )
            if literal is not None and is_self_managed_application(
                application_source_repo_url(literal), report.repo_url,
            ):
                app = literal
        return app is not None, infra_repo_url
    except Exception as exc:
        logger.debug(
            "GitOps registration check failed for %s, falling back to infra_repo_url: %s",
            app_name, exc,
        )
        return infra_repo_url is not None, infra_repo_url


async def deliver_with_verification(
    *,
    mechanism: str,
    files: list[dict],
    report: AssessmentReport,
    app_name: str,
    store: object,
    assessment_id: str,
    actor: str,
    dry_run: bool,
    path_prefix: str | None = None,
    branch_name: str | None = None,
) -> dict:
    """Structurally parallel to ``cluster_apply.apply_with_verification()``,
    for the commit-and-PR delivery mechanisms
    (``infra-repo-commit``/``source-repo-pr``/``app-repo-pr``): one
    ``audit_log()`` call covering every exit path, and
    ``record_skill_outcomes()`` after a successful commit/PR (not just a
    successful apply -- a merged PR is exactly as strong a "this fix was
    accepted" signal, per the design doc).

    ``path_prefix``/``branch_name`` only apply to ``MECHANISM_INFRA_REPO_
    COMMIT`` -- see ``_CICD_SHARED_NAMESPACE_PATH_PREFIX``'s own comment for
    why the CI/CD-shared-namespace category needs both to differ from the
    cluster-config category's own (default) folder/branch, even within the
    same ``route_and_deliver()`` call. ``path_prefix``, when given, overrides
    every file's own generation-source ``category`` for
    ``commit_to_infra_repo()``'s ``apps/{app}/{category}/{filename}`` path --
    on a COPY of each file dict, not ``files`` itself, so
    ``record_skill_outcomes()`` below still attributes outcomes using each
    file's real, original ``category``.
    """
    resource = f"assessment:{assessment_id}"

    if dry_run:
        audit_log(
            actor=actor, action="deliver", resource=resource, outcome="dry-run",
            details={"mechanism": mechanism, "files": len(files)},
        )
        return {"mechanism": mechanism, "dry_run": True, "files": [f["path"] for f in files]}

    try:
        if mechanism == MECHANISM_INFRA_REPO_COMMIT:
            from agentit.portal.github_pr import commit_to_infra_repo, ensure_applicationset

            commit_files = [{**f, "category": path_prefix} for f in files] if path_prefix else files
            result = await asyncio.to_thread(
                commit_to_infra_repo, report.infra_repo_url, app_name, commit_files, branch_name,
            )
            if "error" not in result:
                await asyncio.to_thread(ensure_applicationset, report.infra_repo_url)
                commit_url = result.get("commit_url", "")
                if commit_url:
                    result["commit_sha"] = commit_url.rsplit("/", 1)[-1]
        elif mechanism == MECHANISM_SOURCE_REPO_PR:
            from agentit.portal.github_pr import create_source_patch_pr

            result = await asyncio.to_thread(create_source_patch_pr, report.repo_url, app_name, files)
        elif mechanism == MECHANISM_APP_REPO_PR:
            from agentit.portal.github_pr import create_onboarding_pr

            result = await asyncio.to_thread(create_onboarding_pr, report.repo_url, app_name, files)
        else:
            raise ValueError(f"Unknown delivery mechanism: {mechanism}")
    except Exception as exc:
        audit_log(
            actor=actor, action="deliver", resource=resource, outcome="error",
            details={"mechanism": mechanism, "error": str(exc)[:200]},
        )
        raise

    outcome = "error" if "error" in result else "success"
    audit_log(
        actor=actor, action="deliver", resource=resource, outcome=outcome,
        details={"mechanism": mechanism, "files": len(files),
                  **{k: v for k, v in result.items() if k != "error"}},
    )

    if outcome == "success":
        await record_skill_outcomes(
            store, app_name, files, {f["path"] for f in files}, "approved",
            f"delivered via {mechanism}",
        )

    return {"mechanism": mechanism, "dry_run": False, **result}


def _cicd_shared_namespace_note(files: list[dict]) -> str:
    """The extra callout logged alongside a CI/CD-shared-namespace
    delivery's PR-opened event (see ``_deliver_via_gitops_pr()`` below) --
    names the exact target namespace(s), same as the retired
    ``cluster-admin-review`` gate's own summary used to, so a reviewer
    immediately sees *which* shared namespace this PR affects without
    opening the diff.
    """
    namespaces: set[str] = set()
    for f in files:
        for doc in _parse_manifest(f.get("content", "")):
            ns = (doc.get("metadata") or {}).get("namespace", "")
            if ns:
                namespaces.add(ns)
    ns_list = ", ".join(sorted(namespaces)) or "a shared operator namespace"
    return (
        f"{len(files)} manifest(s) target {ns_list} -- a shared, cluster-wide operator "
        "namespace, not this app's own. Elevated review needed before merging (every app "
        "relying on that namespace is affected, not just this one)."
    )


async def _deliver_via_gitops_pr(
    *,
    files: list[dict],
    report: AssessmentReport,
    app_name: str,
    store: object,
    assessment_id: str,
    actor: str,
    dry_run: bool,
    infra_repo_url: str | None,
    path_prefix: str | None = None,
    branch_name: str | None = None,
    note: str = "",
) -> dict:
    """Commit ``files`` to the GitOps infra repo and open a real PR -- the
    exact sequence the cluster/app-config category has always used, and
    (since 2026-07-18, replacing the removed ``cluster-admin-review``
    direct-apply gate) the CI/CD-shared-namespace category now uses too.
    Shared here so ``route_and_deliver()``'s two call sites below can never
    drift apart on this sequence.

    No gate is created for the opened PR (the ``gates`` table/generic
    gate-resolution machinery has been removed entirely, 2026-07-19): the
    real GitHub PR merge review IS the approval step now, for every
    delivery category equally -- ``pr_tracking.py`` derives "waiting for
    your approval"/merged/rejected purely from the PR's own live GitHub
    state (plus ``pr_outcomes.py``'s durable reject-reason/pre-merge-edit
    capture), and ``routes/pr_actions.py`` provides the real Merge/Close
    actions. This function's only remaining job is committing the files and
    opening the PR; the event logged below (``gitops-pr-opened``) is purely
    an observability record, not anything a human resolves.

    ``path_prefix``/``branch_name`` (see ``_CICD_SHARED_NAMESPACE_PATH_
    PREFIX``) let a caller land this commit in a distinctly-named subfolder/
    branch -- and therefore a distinct PR -- instead of interleaving with the
    app's own cluster-config commit. ``note``, when non-empty, is prepended
    to the logged event's summary as an extra callout (e.g. naming the exact
    shared namespace this PR touches).
    """
    outcome = await deliver_with_verification(
        mechanism=MECHANISM_INFRA_REPO_COMMIT, files=files, report=report,
        app_name=app_name, store=store, assessment_id=assessment_id,
        actor=actor, dry_run=dry_run, path_prefix=path_prefix, branch_name=branch_name,
    )
    pr_url = outcome.get("pr_url") if isinstance(outcome, dict) and not dry_run else None
    if pr_url and "error" not in outcome:
        mechanism_text = confirmation_text(MECHANISM_INFRA_REPO_COMMIT, infra_repo_url=infra_repo_url)
        summary = f"{mechanism_text} PR opened: {pr_url}. Merge it (or Close it) on the Ledger tab -- AgentIT never auto-merges."
        if note:
            summary = f"{note} {summary}"
        try:
            await store.log_event("delivery", "gitops-pr-opened", app_name, "info", summary)
        except Exception:
            logger.warning("Failed to log gitops-pr-opened event for %s", app_name, exc_info=True)
    return outcome


async def _maybe_schedule_verification(
    store: object, delivery_id: str, assessment_id: str, app_name: str,
    namespace: str, mechanism: str, dry_run: bool,
) -> None:
    """Fire-and-forget the shared SLO-watch-and-rollback tail (generalized
    from ``RemediationLoop``) for this delivery, exactly like every other
    delivery through the unified router -- but only when there's an actual
    SLO to watch for this assessment (onboarding's
    ``FleetOrchestrator._create_default_slos()`` always creates one in
    production). This deliberately skips scheduling a 60s background
    verify loop for callers/tests that never set up SLOs, so unit tests
    exercising ``route_and_deliver()`` don't leak dangling asyncio tasks.
    """
    if dry_run or mechanism not in (MECHANISM_DIRECT_APPLY, MECHANISM_INFRA_REPO_COMMIT):
        return
    try:
        slos = await store.list_slos(assessment_id)
    except Exception:
        slos = []
    if not slos:
        return
    asyncio.create_task(
        verify_and_close_delivery(store, delivery_id, assessment_id, app_name, namespace, mechanism)
    )


async def _log_verification_outcome(
    store: object, action: str, app_name: str, severity: str, summary: str,
) -> None:
    """Mirrors ``slo_tracker.py``'s ``rollback-recommended`` event-logging
    pattern (Kafka publish + store-persisted event, the latter wrapped in
    its own best-effort try/except) so a delivery's verification outcome
    produces a real Ledger card/observable event, not just a status-column
    update nobody's watching.
    """
    from agentit.portal.helpers import publish_event
    publish_event(action, app_name, summary, agent_id="delivery-verifier")
    try:
        await store.log_event("delivery-verifier", action, app_name, severity, summary)
    except Exception:
        logger.warning("Failed to log %s event for %s", action, app_name, exc_info=True)


async def verify_and_close_delivery(
    store: object, delivery_id: str, assessment_id: str, app_name: str,
    namespace: str, mechanism: str,
) -> dict:
    """The shared verify step every delivery ends in (docs/unified-apply-
    flow.md section (C)): 60s SLO watch, then close the delivery row as
    verified, or roll back (direct-apply) / report-and-stop (GitOps --
    rollback semantics for that branch are explicitly out of scope, see the
    design doc's "Deliberately not addressed" #3: reverting a merged commit
    and waiting for a second human merge is a real, honestly-named gap, not
    something this router auto-shortcuts).
    """
    from agentit.remediation_loop import rollback_action, verify_slos

    try:
        result = await verify_slos(store, assessment_id, app_name)
    except Exception as exc:
        logger.warning("Verification failed for delivery %s: %s", delivery_id, exc)
        await store.update_delivery(delivery_id, details={"verify_error": str(exc)})
        return {"healthy": None, "error": str(exc)}

    if result["healthy"]:
        await store.update_delivery(delivery_id, status="verified", verification="verified")
        await _log_verification_outcome(
            store, "delivery-verified", app_name, "info",
            f"Delivery {delivery_id} verified healthy after {mechanism} -- no SLO breach detected",
        )
        return result

    if mechanism == MECHANISM_DIRECT_APPLY:
        rb = await rollback_action(app_name, namespace)
        await store.update_delivery(
            delivery_id, status="rolled_back", verification="breached",
            details={"rollback": rb, "breach_reason": result.get("reason")},
        )
        await _log_verification_outcome(
            store, "delivery-rolled-back", app_name, "critical",
            f"Delivery {delivery_id} breached SLOs after direct apply -- "
            f"rolled back automatically ({result.get('reason')})",
        )
    else:
        await store.update_delivery(
            delivery_id, status="breach-reported", verification="breached",
            details={"breach_reason": result.get("reason")},
        )
        await _log_verification_outcome(
            store, "delivery-breach-reported", app_name, "critical",
            f"Delivery {delivery_id} breached SLOs after {mechanism} -- no automatic "
            f"rollback for GitOps deliveries ({result.get('reason')}); manual review needed",
        )
    return result


async def route_and_deliver(
    files: list[dict],
    *,
    app_name: str,
    namespace: str,
    report: AssessmentReport | None,
    store: object,
    assessment_id: str,
    actor: str,
    dry_run: bool,
    target_findings: list[tuple[str, str]] | None = None,
) -> dict:
    """The one decision surface for "does this change reach a cluster/repo
    now" -- classify, look up GitOps registration, and route each
    classified group to exactly one delivery mechanism. Every one of the
    design doc's six entry points (manual apply, gate-approve, AutoMode,
    DriftDetector's future new-fix path, Create PR, Per-Agent PRs) funnels
    through this instead of independently calling
    ``apply_manifests_to_cluster``/``apply_with_verification``/
    ``create_onboarding_pr``/``commit_to_infra_repo`` on their own.

    ``assessment_id`` is required (not in the design doc's illustrative
    signature) because every side effect here -- SLOs, audit resources, the
    ``deliveries`` row itself -- is keyed by it.

    No longer takes a ``force_dry_run_first`` parameter -- that was
    AutoMode's own safety knob for the cluster-config direct-apply branch's
    forced dry-run-then-real-apply sequence, removed along with Direct
    Apply/AutoMode's direct-apply branch as a concept entirely (see
    ``resolve_cluster_config_mechanism()``/``automode.py``'s simplified
    ``execute()``).

    ``target_findings`` (docs/onboarding-loop-vision-gap-analysis.md Phase
    3), when the caller knows which specific finding(s) this batch of
    ``files`` was generated to resolve, is recorded verbatim on the
    ``deliveries`` row (``store.create_delivery()``) as the exact
    ``(category, description.lower()[:80])`` key
    ``assessment_diff.diff_assessments()`` dedups findings on -- so a later
    re-assessment's diff can be correlated back to this one delivery
    (``correlate_delivery_finding()`` below) to answer "did this delivery's
    target finding actually clear." Omitted (the default) for callers that
    don't have one or a few specific findings in mind for this batch (e.g.
    a dry-run-only call, or a batch spanning many findings at once where no
    single one is "the" target) -- ``list_deliveries_pending_finding_check()``
    simply never returns those rows, so nothing downstream breaks.
    """
    groups: dict[str, list[dict]] = {}
    for f in files:
        groups.setdefault(classify_file(f), []).append(f)

    # Delivered-content traceability (requirement: a human-edited file must
    # be a permanent, queryable fact about what was actually delivered, not
    # a transient UI-only detail) -- `classify_file` above already
    # classified each entry off its *current* `content` (whatever
    # `update_onboarding_file()` last persisted, edited or not), so the
    # taxonomy/routing decision above this line already reacted to the real
    # edited content, not the original generation. This just records which
    # of the delivered files carry the `edited` flag `update_onboarding_file`
    # sets, so `deliveries.details_json.edited_files` answers "was any of
    # this delivery's content edited from what was originally generated"
    # without a human needing to diff the file themselves.
    edited_files = [f["path"] for f in files if f.get("edited")]

    blocked = groups.pop(CATEGORY_SECRET_BLOCKED, [])
    for f in blocked:
        logger.error(
            "Delivery blocked: %s classified as kind=Secret -- never routed to any "
            "delivery mechanism (see docs/unified-apply-flow.md's permanent deny-rule)",
            f.get("path"),
        )

    excluded = groups.pop(CATEGORY_NARRATIVE_REPORT, [])

    # Strip unsubstituted placeholders from every remaining category before
    # mechanism selection so dry-run and real Deliver agree on what ships.
    placeholder_blocked: list[dict] = []
    for cat, fs in list(groups.items()):
        keep: list[dict] = []
        for f in fs:
            if has_unresolved_placeholders(f.get("content")):
                placeholder_blocked.append(f)
                logger.error(
                    "Delivery blocked: %s still contains unresolved placeholder "
                    "(e.g. REPLACE_WITH_AGENTIT_IMAGE) -- refusing to route",
                    f.get("path"),
                )
            else:
                keep.append(f)
        if keep:
            groups[cat] = keep
        else:
            groups.pop(cat, None)

    registered, infra_repo_url = await is_gitops_registered(app_name, report)

    categories_summary: dict[str, int] = {cat: len(fs) for cat, fs in groups.items()}
    if blocked:
        categories_summary[CATEGORY_SECRET_BLOCKED] = len(blocked)
    if excluded:
        categories_summary[CATEGORY_NARRATIVE_REPORT] = len(excluded)
    if placeholder_blocked:
        categories_summary["placeholder_blocked"] = len(placeholder_blocked)

    mechanisms: dict[str, str] = {}
    outcomes: dict[str, object] = {}

    # Direct Apply has been removed as a concept entirely -- every
    # cluster/app-config delivery either commits to a known infra repo
    # (bootstrapping apps/{app}/ on the very first delivery, see
    # resolve_cluster_config_mechanism()'s docstring) or refuses outright
    # when no infra repo is known at all (only possible for an assessment
    # saved before GitOps registration became mandatory).
    cluster_files = groups.pop(CATEGORY_CLUSTER_CONFIG, [])
    if cluster_files:
        mechanisms[CATEGORY_CLUSTER_CONFIG] = resolve_cluster_config_mechanism(infra_repo_url)

    # CI/CD manifests destined for a shared operator namespace (2026-07-18):
    # no longer a cluster-admin-review direct-apply gate (removed entirely --
    # AgentIT never applies directly to a cluster for any category now).
    # ArgoCD's own reconciler service account already holds the elevated
    # RBAC AgentIT's lacks for these namespaces (verified live against this
    # cluster's openshift-gitops-argocd-application-controller SA -- see the
    # README), so this resolves through the exact same GitOps-commit
    # decision as the cluster-config category above.
    cicd_files = groups.pop(CATEGORY_CICD_SHARED_NAMESPACE, [])
    if cicd_files:
        mechanisms[CATEGORY_CICD_SHARED_NAMESPACE] = resolve_cluster_config_mechanism(infra_repo_url)

    source_files = groups.pop(CATEGORY_SOURCE_PATCH, [])
    if source_files:
        mechanisms[CATEGORY_SOURCE_PATCH] = MECHANISM_SOURCE_REPO_PR

    at_rest_files = groups.pop(CATEGORY_MANIFEST_AT_REST, [])
    if at_rest_files:
        mechanisms[CATEGORY_MANIFEST_AT_REST] = MECHANISM_APP_REPO_PR

    delivery_id = await store.create_delivery(
        assessment_id, app_name, categories_summary,
        mechanism=",".join(f"{c}:{m}" for c, m in mechanisms.items()) or MECHANISM_NONE,
        status="in_progress",
        details={
            "registered": registered, "infra_repo_url": infra_repo_url, "dry_run": dry_run,
            "edited_files": edited_files,
            "confirmation_text": {
                cat: confirmation_text(m, infra_repo_url=infra_repo_url) for cat, m in mechanisms.items()
            },
        },
        target_findings=target_findings,
    )

    # Everything below can raise (cluster API calls, GitHub PR creation,
    # namespace checks) -- without this try/except, an exception here left
    # the just-created row stuck at status="in_progress" forever: the
    # caller's error banner correctly told the human the delivery failed,
    # but Delivery History kept showing an eternally in-progress row for the
    # same attempt (confirmed live: two failed "Apply to Cluster" attempts
    # against an unreachable cluster left two permanently in_progress rows,
    # never "failed").
    try:
        if cluster_files:
            if mechanisms[CATEGORY_CLUSTER_CONFIG] == MECHANISM_INFRA_REPO_COMMIT and report is not None:
                # Opens the PR; a human merges (or closes) it directly from
                # the Actions tab (routes/pr_actions.py) -- the real GitHub
                # PR review IS the approval step now, no gate involved.
                outcomes[CATEGORY_CLUSTER_CONFIG] = await _deliver_via_gitops_pr(
                    files=cluster_files, report=report, app_name=app_name, store=store,
                    assessment_id=assessment_id, actor=actor, dry_run=dry_run,
                    infra_repo_url=infra_repo_url,
                )
            else:
                # MECHANISM_NONE: no infra_repo_url is known for this
                # assessment at all -- Direct Apply is no longer a fallback
                # (removed as a concept entirely), so this refuses rather
                # than mutating the cluster or guessing which repo to commit
                # to. Only reachable for an assessment saved before GitOps
                # registration became mandatory; a human must register this
                # app for GitOps (e.g. "Register for GitOps" on Assessment
                # Detail) before it can be delivered at all.
                outcomes[CATEGORY_CLUSTER_CONFIG] = {
                    "error": (
                        "no GitOps infra repo is known for this assessment -- Direct Apply has "
                        "been removed, so this cannot be delivered until the app is registered "
                        "for GitOps (see \"Register for GitOps\" on Assessment Detail)"
                    ),
                }

        if cicd_files:
            # Retired (2026-07-18): this used to open a cluster-admin-review
            # gate whose approval performed a real, elevated-RBAC direct
            # apply -- AgentIT's own service account structurally lacked
            # RBAC for these namespaces (still true), but that's no longer
            # the point: AgentIT never applies directly to a cluster for ANY
            # category now. ArgoCD's reconciler SA already holds the RBAC
            # AgentIT lacks (verified live, see the README), so this commits
            # to a distinctly-named path/branch (never the cluster-config
            # category's own, even within this same call -- see
            # _CICD_SHARED_NAMESPACE_PATH_PREFIX) and opens its own PR,
            # merged/closed the same way as cluster-config's.
            if mechanisms[CATEGORY_CICD_SHARED_NAMESPACE] == MECHANISM_INFRA_REPO_COMMIT and report is not None:
                outcomes[CATEGORY_CICD_SHARED_NAMESPACE] = await _deliver_via_gitops_pr(
                    files=cicd_files, report=report, app_name=app_name, store=store,
                    assessment_id=assessment_id, actor=actor, dry_run=dry_run,
                    infra_repo_url=infra_repo_url,
                    path_prefix=_CICD_SHARED_NAMESPACE_PATH_PREFIX,
                    branch_name=f"agentit/{_sanitize_app_name(app_name)}-cicd-shared-namespace",
                    note=_cicd_shared_namespace_note(cicd_files),
                )
            else:
                outcomes[CATEGORY_CICD_SHARED_NAMESPACE] = {
                    "error": (
                        "no GitOps infra repo is known for this assessment -- CI/CD manifests "
                        "destined for a shared operator namespace can only be delivered via a "
                        "GitOps PR now (the elevated-RBAC direct-apply path has been removed); "
                        "register this app for GitOps first (see \"Register for GitOps\" on "
                        "Assessment Detail)"
                    ),
                }

        if source_files:
            if report is not None:
                outcomes[CATEGORY_SOURCE_PATCH] = await deliver_with_verification(
                    mechanism=MECHANISM_SOURCE_REPO_PR, files=source_files, report=report,
                    app_name=app_name, store=store, assessment_id=assessment_id,
                    actor=actor, dry_run=dry_run,
                )
            else:
                outcomes[CATEGORY_SOURCE_PATCH] = {"error": "no assessment report available -- cannot open a source-repo PR"}

        if at_rest_files:
            if report is not None:
                outcomes[CATEGORY_MANIFEST_AT_REST] = await deliver_with_verification(
                    mechanism=MECHANISM_APP_REPO_PR, files=at_rest_files, report=report,
                    app_name=app_name, store=store, assessment_id=assessment_id,
                    actor=actor, dry_run=dry_run,
                )
            else:
                outcomes[CATEGORY_MANIFEST_AT_REST] = {"error": "no assessment report available"}

        any_error = any(isinstance(o, dict) and "error" in o for o in outcomes.values())
        overall_status = "delivered" if not any_error else "partial"
        await store.update_delivery(
            delivery_id, status=overall_status,
            details={"outcomes": {k: v for k, v in outcomes.items()}},
        )

        if cluster_files:
            mechanism_used = mechanisms[CATEGORY_CLUSTER_CONFIG]
            await _maybe_schedule_verification(
                store, delivery_id, assessment_id, app_name, namespace, mechanism_used, dry_run,
            )
    except Exception as exc:
        await store.update_delivery(
            delivery_id, status="failed", details={"error": str(exc)[:500]},
        )
        raise

    return {
        "delivery_id": delivery_id,
        "registered": registered,
        "infra_repo_url": infra_repo_url,
        "mechanisms": mechanisms,
        "outcomes": outcomes,
        "blocked": [f["path"] for f in blocked],
        "placeholder_blocked": [f["path"] for f in placeholder_blocked],
        "excluded": [f["path"] for f in excluded],
    }


# ── Finding-scoped re-verification & bounded auto-escalation ──────────────
# (docs/onboarding-loop-vision-gap-analysis.md §7/Phase 3-4). Nothing above
# this line changes shape for a caller that never passes `target_findings`.
# FINDING_ESCALATION_THRESHOLD is defined near the top of this module.


async def correlate_delivery_finding(
    store: object, delivery: dict, new_report: AssessmentReport | None,
) -> dict:
    """Phase 3's finding-scoped re-verification: did THIS delivery's target
    finding(s) (``delivery["target_findings"]``, set at delivery-creation
    time by ``route_and_deliver()``) actually clear, per a subsequent
    re-assessment?

    Returns ``{"status": ..., "target_findings": [...],
    "still_present_findings": [...], "resolved_findings": [...]}``, where
    ``status`` is one of:

    - ``"resolved"`` -- every target finding is gone from ``new_report``
      (whatever delivered actually fixed it, or it disappeared for some
      other reason -- either way, it's not there anymore).
    - ``"still_present"`` -- at least one target finding is still present in
      ``new_report``, unchanged or not -- the fix did not clear it.
    - ``"pending"`` -- no subsequent assessment exists yet to check against
      (``new_report is None``).
    - ``"unknown"`` -- this delivery never recorded any target findings at
      all (most historical/whole-batch deliveries), so there's nothing to
      correlate.

    Deliberately checks membership in ``new_report``'s own, current finding
    set (via ``assessment_diff.current_finding_keys()``) rather than only
    consulting a pre-computed ``AssessmentDiff``'s ``new_findings``/
    ``resolved_findings`` lists: those two lists only cover findings that
    *changed* between the two assessments, so a finding that's still
    present completely unchanged -- the expected shape of "the fix didn't
    work at all" -- would appear in neither list and be invisible to a
    diff-only check.
    """
    target_findings = delivery.get("target_findings") or []
    if not target_findings:
        return {"status": "unknown", "target_findings": [], "still_present_findings": [], "resolved_findings": []}
    if new_report is None:
        return {
            "status": "pending", "target_findings": target_findings,
            "still_present_findings": [], "resolved_findings": [],
        }

    from agentit.assessment_diff import current_finding_keys

    target_keys = {tuple(k) for k in target_findings}
    current_keys = current_finding_keys(new_report)
    still_present = sorted(target_keys & current_keys)
    resolved = sorted(target_keys - current_keys)
    status = "still_present" if still_present else "resolved"
    return {
        "status": status, "target_findings": target_findings,
        "still_present_findings": still_present, "resolved_findings": resolved,
    }


async def _log_finding_resolution_outcome(
    store: object, app_name: str, action: str, severity: str, summary: str,
) -> str | None:
    """Mirrors ``_log_verification_outcome()`` above (introduced for the
    SLO-verification tail, 69e09b9) for the finding-resolution tail -- same
    Kafka-publish-plus-store-event pattern, same best-effort try/except
    around the store write, so a finding's resolved/still-present/escalated
    outcome produces a real Ledger card/observable event instead of only a
    column update nobody's watching. Returns the new event's id (or
    ``None`` if the store write itself failed) -- ``escalate_unresolved_
    finding()`` below needs it as the real recommendation identity a human
    later acknowledges (``routes/recommendations.py``).
    """
    from agentit.portal.helpers import publish_event
    publish_event(action, app_name, summary, agent_id="delivery-verifier")
    try:
        return await store.log_event("delivery-verifier", action, app_name, severity, summary)
    except Exception:
        logger.warning("Failed to log %s event for %s", action, app_name, exc_info=True)
        return None


def _describe_finding(key: tuple[str, str]) -> str:
    category, desc_key = key[0], key[1]
    return f"{category} ('{desc_key}')"


async def escalate_unresolved_finding(
    store: object, assessment_id: str, app_name: str, finding: tuple[str, str], failure_count: int,
) -> str | None:
    """Phase 4's stop condition: a real, visible "needs you" signal for a
    finding that has now failed to resolve ``failure_count`` (>=
    ``FINDING_ESCALATION_THRESHOLD``) times in a row -- a plain
    ``finding-escalated`` event (see ``routes/recommendations.py``'s
    ``acknowledge_finding_escalation``), not a generic gate row. Not a
    silent give-up and not another identical auto-retry either way.

    Deduped per ``(app_name, category)`` via ``store.list_unresolved_
    events()`` -- the same "don't pile up duplicates for the same still-
    unresolved thing" job the retired ``gates`` table's ``(repo_url,
    gate_type)`` dedup used to do (this is actually a correctness
    improvement over that: the old dedup was keyed on ``gate_type`` alone,
    so two DIFFERENT finding categories escalating for the same app would
    have collided into a single gate -- the second escalation silently
    never got its own row). Returns the existing unresolved event's id when
    one for this exact category is already open, or the newly-logged
    event's id otherwise.
    """
    category, desc_key = finding[0], finding[1]

    existing = await store.list_unresolved_events(
        "finding-escalated", ["finding-escalation-acknowledged"], target_app=app_name,
    )
    for event in existing:
        if _escalation_event_category(event.get("summary", "")) == category:
            return event["id"]

    summary = (
        f"'{category}' finding has failed to resolve after {failure_count} automated fix "
        f"attempt(s) -- human review needed. Target finding: {desc_key}"
    )
    return await _log_finding_resolution_outcome(store, app_name, "finding-escalated", "critical", summary)


async def redispatch_finding_fix(
    store: object, report: AssessmentReport, assessment_id: str,
    app_name: str, finding: tuple[str, str],
) -> dict:
    """Phase 4's below-threshold branch: re-dispatch a fresh fix attempt for
    this exact finding through the same mechanism the original fix went
    through -- ``RemediationDispatcher.dispatch()`` to generate, then
    ``route_and_deliver()`` directly to deliver (AutoMode has been removed;
    it never did anything beyond an extra LLM safety classification on top
    of this exact call for the cluster-config category -- see its own
    former module docstring) -- reusing the auto-chain machinery this same
    effort already wired end-to-end (``webhook_github_push``'s
    auto-fixable loop, Phase 0's 9ccfa21) rather than re-implementing
    delivery logic here.

    A category's skill/template renders deterministically (no LLM in the
    generation step itself, see ``RemediationDispatcher``'s module comment)
    -- a retry with no other state change will typically regenerate the
    exact same fix. This bounded retry exists for the cases where that's
    still enough (a transient delivery failure, a race, a since-changed
    repo state), not as a mechanism for trying a materially different fix;
    a structurally wrong fix is exactly what ``FINDING_ESCALATION_THRESHOLD``
    -- not this function -- is meant to catch. Delivering a cluster-config
    fix here still only ever opens a GitOps PR gated on a human merge,
    never a direct cluster mutation.
    """
    category = finding[0]
    from agentit.remediation.dispatcher import RemediationDispatcher

    dispatcher = RemediationDispatcher(store)
    dispatch_result = await dispatcher.dispatch(assessment_id, category, app_name)
    if not dispatch_result["files"]:
        reason = dispatch_result.get("error") or "dispatcher produced no files"
        await _log_finding_resolution_outcome(
            store, app_name, "finding-redispatch-no-fix", "warning",
            f"Re-dispatch for '{category}' produced no fix: {reason}",
        )
        return {"action": "no-fix-available", "reason": reason}

    namespace = app_name.lower().replace("_", "-").replace(".", "-")
    delivery = await route_and_deliver(
        dispatch_result["files"], app_name=app_name, namespace=namespace, report=report,
        store=store, assessment_id=assessment_id, actor="delivery-verifier",
        dry_run=False, target_findings=[finding],
    )
    cluster_outcome = delivery["outcomes"].get(CATEGORY_CLUSTER_CONFIG)
    if isinstance(cluster_outcome, dict) and cluster_outcome.get("pr_url"):
        action = "delivered"
        reason = f"Opened PR {cluster_outcome.get('pr_url', '')} -- awaiting human merge".strip()
    elif isinstance(cluster_outcome, dict) and cluster_outcome.get("error"):
        action, reason = "routing-error", cluster_outcome["error"]
    else:
        action, reason = "delivered", "Delivered via the unified router"

    await _log_finding_resolution_outcome(
        store, app_name, "finding-redispatched", "info",
        f"Re-dispatched a fresh fix for '{category}' after a confirmed still-present finding: "
        f"{action} -- {reason}",
    )
    return {"action": action, "reason": reason, "agent": dispatch_result["agent"]}


async def handle_confirmed_finding_failure(
    store: object, report: AssessmentReport, assessment_id: str,
    app_name: str, finding: tuple[str, str],
) -> dict:
    """Phase 4's single decision point, called once per still-present
    target finding from ``check_pending_delivery_verifications()`` below:
    below ``FINDING_ESCALATION_THRESHOLD`` confirmed failures for this
    (app, finding-category), re-dispatch a fresh attempt; at or above it,
    stop and escalate instead. This is the one place that decision is made
    -- not scattered across ``RemediationLoop``/``VulnWatcher`` (a
    structurally separate, watcher-triggered pipeline this phase never
    touches -- neither one has any relationship to ``deliveries.
    target_findings``/``finding_resolution`` today) or any other call site.
    """
    category = finding[0]
    failure_count = await store.get_finding_failure_count(app_name, category)
    if failure_count >= FINDING_ESCALATION_THRESHOLD:
        event_id = await escalate_unresolved_finding(store, assessment_id, app_name, finding, failure_count)
        return {"action": "escalated", "event_id": event_id, "failure_count": failure_count}
    # Nested (not flattened via `**result`) deliberately: redispatch_finding_
    # fix() returns its own "action" key (the underlying delivery outcome,
    # e.g. "delivered"/"routing-error") -- flattening it would silently
    # overwrite this function's own "redispatched" vs. "escalated"
    # decision, the one thing a caller most needs to tell apart.
    result = await redispatch_finding_fix(store, report, assessment_id, app_name, finding)
    return {"action": "redispatched", "failure_count": failure_count, "redispatch_result": result}


async def check_pending_delivery_verifications(
    store: object, app_name: str, new_report: AssessmentReport, new_assessment_id: str,
) -> list[dict]:
    """Phase 3 + Phase 4's single entry point, called from
    ``webhook_github_push``'s existing diff-triggered re-assessment flow
    (routes/webhooks.py) for every push-triggered re-assessment: for every
    delivery on this app awaiting a finding check
    (``store.list_deliveries_pending_finding_check()``), correlate against
    ``new_report`` and persist+log the real outcome (Phase 3), then react
    to a confirmed ``"still_present"`` outcome (Phase 4: bounded auto-
    retry, then escalate).

    Both phases are unconditional -- there is no more ``auto_mode`` toggle
    to gate Phase 4 behind (AutoMode has been removed): a still-present
    target finding always either re-dispatches a fresh fix attempt (below
    ``FINDING_ESCALATION_THRESHOLD`` confirmed failures) or escalates to a
    real, visible human-review gate (at or above it) -- see
    ``handle_confirmed_finding_failure()``.
    """
    pending = await store.list_deliveries_pending_finding_check(app_name)
    results: list[dict] = []
    for d in pending:
        outcome = await correlate_delivery_finding(store, d, new_report)
        if outcome["status"] not in ("resolved", "still_present"):
            continue

        await store.update_delivery(d["id"], finding_resolution=outcome["status"])

        if outcome["status"] == "resolved":
            resolved_desc = ", ".join(_describe_finding(k) for k in outcome["resolved_findings"]) or "target finding"
            await _log_finding_resolution_outcome(
                store, app_name, "delivery-finding-resolved", "info",
                f"Delivery {d['id']} confirmed resolved: {resolved_desc} no longer present on re-assessment",
            )
            results.append({"delivery_id": d["id"], **outcome})
            continue

        still_present_desc = ", ".join(_describe_finding(k) for k in outcome["still_present_findings"])
        resolved_note = ""
        if outcome["resolved_findings"]:
            resolved_note = " (" + ", ".join(_describe_finding(k) for k in outcome["resolved_findings"]) + " did resolve)"
        await _log_finding_resolution_outcome(
            store, app_name, "delivery-finding-still-present", "warning",
            f"Delivery {d['id']} did NOT resolve: {still_present_desc} still present on re-assessment{resolved_note}",
        )

        escalations = []
        for key in outcome["still_present_findings"]:
            escalations.append(await handle_confirmed_finding_failure(
                store, new_report, new_assessment_id, app_name, tuple(key),
            ))
        results.append({"delivery_id": d["id"], **outcome, "escalations": escalations})
    return results


# ── Phase 5: a real "what happens next" fact, per app ──────────────────────
# (docs/onboarding-loop-vision-gap-analysis.md's Step 8 discussion). Purely a
# read-side view over Phase 3/4's own data -- no new tables, no new writes --
# answering, for one app, which of the states below is currently true.
NEXT_ACTION_ESCALATED = "escalated"
NEXT_ACTION_RETRYING = "retrying"
NEXT_ACTION_PENDING_VERIFICATION = "pending_verification"
NEXT_ACTION_NONE = "none"

_ESCALATION_CATEGORY_RE = re.compile(r"^'([^']+)'")


def _escalation_event_category(summary: str) -> str:
    """Recover the finding category ``escalate_unresolved_finding()`` named
    in its own event summary (``"'{category}' finding has failed to
    resolve ..."``) -- plain ``events`` rows have no structured category
    column of their own, and this deterministic summary shape, produced by
    this same module, is the one place that category still lives. Used both
    to dedup a repeat escalation for the same (app, category) and to label
    the "what happens next" state below.
    """
    match = _ESCALATION_CATEGORY_RE.match(summary or "")
    return match.group(1) if match else "finding"


async def get_next_action_state(
    store: object,
    app_name: str,
    *,
    repo_url: str | None = None,
    assessment_id: str | None = None,
    unresolved_escalations: list[dict] | None = None,
) -> dict:
    """The one real "what happens next" fact for ``app_name`` -- reusing
    Phase 3/4's own data access (``store.list_unresolved_events``/
    ``list_deliveries_pending_finding_check``/``get_finding_failure_count``)
    rather than a new query, and never inventing a re-check cadence that
    doesn't exist.

    Checked in priority order, since these three states are (by
    construction, see ``handle_confirmed_finding_failure()``) close to
    mutually exclusive per finding but a real app can have more than one
    finding in flight at once:

    1. ``NEXT_ACTION_ESCALATED`` -- an unresolved ``finding-escalated``
       event is open for this app: automated retries are exhausted for some
       finding, a human is needed now. Takes priority over the other two
       because it's the one state that actually requires a person to act.
    2. ``NEXT_ACTION_RETRYING`` -- a pending (not yet finding-checked)
       delivery exists whose target finding has already failed at least
       once (``get_finding_failure_count() > 0``) -- i.e. this pending
       delivery IS a bounded auto-retry already in flight, awaiting the next
       push to verify it.
    3. ``NEXT_ACTION_PENDING_VERIFICATION`` -- a pending delivery exists
       whose target finding has never failed before -- ordinary, first-time
       "wait for the next push" verification.
    4. ``NEXT_ACTION_NONE`` -- nothing pending or failing right now. There is
       no periodic re-check to report here: ``webhook_github_push`` only
       ever re-assesses on a push to the app's own repo (see docs/
       onboarding-loop-vision-gap-analysis.md §8) -- nothing re-assesses a
       clean app on any schedule, so this state says that plainly rather
       than implying a cadence that doesn't exist.

    ``unresolved_escalations``, when given, is an already-fetched
    unresolved-escalation-events list (either fleet-wide via
    ``list_unresolved_events(..., target_app=None)``, or already scoped to
    this app) -- lets a caller enriching many apps at once (Fleet) fetch it
    once instead of once per app. Left ``None`` (the default), this fetches
    its own, scoped to ``app_name`` -- the right choice for a single-app
    caller. ``assessment_id``, when given, is embedded verbatim into the
    escalated state's own dict so a caller (Fleet's badge, Assessment
    Detail) can link straight to that app's Actions tab.
    """
    if unresolved_escalations is None:
        try:
            unresolved_escalations = await store.list_unresolved_events(
                "finding-escalated", ["finding-escalation-acknowledged"], target_app=app_name,
            )
        except Exception:
            logger.debug("Failed to fetch unresolved escalations for %s's next-action state", app_name, exc_info=True)
            unresolved_escalations = []

    # A fleet-wide, unscoped list needs its own target_app filter; an
    # already-per-app-scoped one is already exactly this app's events.
    escalation_event = next(
        (e for e in unresolved_escalations if e.get("target_app", app_name) == app_name), None,
    )
    if escalation_event is not None:
        category = _escalation_event_category(escalation_event.get("summary", ""))
        return {
            "state": NEXT_ACTION_ESCALATED,
            "label": "Needs review",
            "message": f"Needs your review -- automated fixes exhausted for '{category}'.",
            "category": category,
            "event_id": escalation_event["id"],
            "assessment_id": assessment_id,
        }

    pending_deliveries = await store.list_deliveries_pending_finding_check(app_name)
    if not pending_deliveries:
        return {
            "state": NEXT_ACTION_NONE,
            "label": None,
            "message": (
                "Nothing pending or failing for this app right now -- AgentIT only re-checks on "
                "the next push to this app's repo (or a manual re-Assess); there's no periodic "
                "re-check on a schedule."
            ),
        }

    worst_category: str | None = None
    worst_failure_count = -1
    for d in pending_deliveries:
        for key in d.get("target_findings") or []:
            category = key[0]
            failure_count = await store.get_finding_failure_count(app_name, category)
            if failure_count > worst_failure_count:
                worst_failure_count = failure_count
                worst_category = category

    if worst_failure_count > 0:
        return {
            "state": NEXT_ACTION_RETRYING,
            "label": f"Retry {worst_failure_count} of {FINDING_ESCALATION_THRESHOLD}",
            "message": (
                f"Retry {worst_failure_count} of {FINDING_ESCALATION_THRESHOLD} -- AgentIT will "
                f"re-attempt this fix automatically for '{worst_category}'."
            ),
            "category": worst_category,
            "failure_count": worst_failure_count,
        }

    repo_label = repo_url or app_name
    return {
        "state": NEXT_ACTION_PENDING_VERIFICATION,
        "label": "Awaiting verification",
        "message": f"Awaiting verification -- will check on next push to `{repo_label}`.",
        "repo": repo_label,
    }
