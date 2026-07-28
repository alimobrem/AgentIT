"""Assess pipeline: resolving the mandatory GitOps infra repo, the real
clone+assess work, and the background-thread orchestration ``POST
/assess`` kicks off.

Moved out of ``routes/assessments.py`` (2026-07-20 reuse/refactor review)
verbatim -- every function here is byte-for-byte the same code that used
to live inline in that file, just relocated, with one new function,
``start_assess_job()``, holding the exact background-thread body that
used to be a closure inside ``assess_submit()`` itself.

**Read this before touching ``start_assess_job()``.** It is the trickiest
part of this whole split: ``assess_submit()``'s background thread creates
a job, then (once the assessment completes) schedules
``onboard_pipeline._run_onboarding_job()`` via
``asyncio.run_coroutine_threadsafe(..., loop)`` onto the portal's
persistent event loop. ``AssessmentStore``'s ``asyncpg`` connection pool
is bound to the event loop that created it and can't be driven from a
different thread's loop, so every store call made from the background
thread is scheduled back onto the *request's* event loop via that same
``run_coroutine_threadsafe`` pattern (see ``_bridge()`` below). Moving
this into its own module changes nothing about that: ``start_assess_job()``
is an ``async def`` awaited directly by ``assess_submit()`` on the exact
same event loop/thread as before, so ``asyncio.get_running_loop()``
inside it still captures the same loop the original inline code did --
calling it from a different (but still coroutine-awaited, same-thread)
stack frame does not change which loop is "running" for
``asyncio.get_running_loop()``'s purposes. The ``threading.Thread(target=
_run, daemon=True).start()`` call, the ``_bridge()`` closure, and the
``_run()`` closure's body are otherwise untouched line-for-line from the
original ``assess_submit()``.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import threading

from fastapi import Request

from agentit.cloner import clone_repo
from agentit.portal.helpers import _get_trusted_base_url, get_llm_client, publish_event
from agentit.portal.services.onboard_pipeline import _run_onboarding_job
from agentit.runner import run_assessment

log = logging.getLogger(__name__)


def _clone_assess_cleanup(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
    secret_decisions_out: list[dict] | None = None,
    secret_classify_cache: object | None = None,
):
    # Same process-wide slot webhook assesses use (helpers.assess_concurrency_slot)
    # so a UI Assess click cannot race a GitHub push into a dual-clone OOM.
    from agentit.portal.helpers import assess_concurrency_slot
    with assess_concurrency_slot():
        repo_path = clone_repo(repo_url)
        try:
            return run_assessment(
                repo_path, repo_url, criticality,
                llm_client=get_llm_client(), infra_repo_url=infra_repo_url,
                check_results_out=check_results_out,
                secret_decisions_out=secret_decisions_out,
                secret_classify_cache=secret_classify_cache,
            )
        finally:
            shutil.rmtree(repo_path, ignore_errors=True)


class InfraRepoRequiredError(Exception):
    """Raised when a real GitOps infra repo can't be resolved for a new
    assessment -- the product directive that all apps must use GitOps means
    this is a hard stop on Assess, never a fallback to Direct Apply. Carries
    a human-readable, actionable message (trusted-domain rejection, a
    repo-creation permission error, etc.) shown verbatim on the failed job.
    """


def _resolve_mandatory_infra_repo_url(repo_url: str, human_supplied: str | None) -> str:
    """Resolve a real, usable GitOps infra repo URL for a brand-new
    assessment -- auto-created via ``_auto_create_infra_repo()`` when the
    human didn't supply one, otherwise a human-supplied bring-your-own
    GitOps repo, verified/created via ``github_pr.ensure_custom_gitops_repo()``.
    Either way the result is validated against the same trusted-git-host
    allowlist ``ensure_applicationset()`` enforces at first-delivery time
    (``github_pr.is_trusted_git_host()``), so an untrusted or unusable infra
    repo is rejected here -- at Assess time -- rather than silently accepted
    only to discover, much later, that GitOps sync will never actually work.

    Raises ``InfraRepoRequiredError`` (never returns ``None``/falls back to
    Direct Apply) on any failure -- all apps must be GitOps-registered now.
    """
    from agentit.portal.github_pr import ensure_custom_gitops_repo, is_trusted_git_host

    if human_supplied:
        if not is_trusted_git_host(human_supplied):
            raise InfraRepoRequiredError(
                f"GitOps infra repo '{human_supplied}' is not on a trusted Git host "
                "(set AGENTIT_TRUSTED_GIT_DOMAINS if it should be) -- Assess cannot "
                "proceed without a usable GitOps infra repo."
            )
        # Bring-your-own-repo: real existence + write-access check, creating
        # it (empty, in the exact org the URL specifies) only if missing or
        # inaccessible -- never a silent no-op on an unusable custom repo.
        # See `ensure_custom_gitops_repo()`'s docstring for the three cases.
        result = ensure_custom_gitops_repo(human_supplied)
        if "error" in result:
            raise InfraRepoRequiredError(
                f"GitOps infra repo '{human_supplied}' could not be used: {result['error']}"
            )
        return result["repo_url"]

    infra = _auto_create_infra_repo(repo_url)
    if infra is None:
        raise InfraRepoRequiredError(
            "Could not auto-create a GitOps infra repo for this app (often a "
            "GITHUB_TOKEN permissions issue, or the repo's GitHub org/token doesn't "
            "allow AgentIT to create a private repo there) -- all apps must be "
            "GitOps-registered now, with no Direct Apply fallback. Supply a GitOps "
            "Infra Repo URL manually and retry Assess."
        )
    if not is_trusted_git_host(infra):
        # Nothing in the request handed us this URL -- it came back from our
        # own _auto_create_infra_repo()/GitHub API call -- so this branch is
        # only reachable if AGENTIT_TRUSTED_GIT_DOMAINS was narrowed below the
        # default GitHub host ensure_infra_repo() itself always creates
        # against. Still validated (never assumed) rather than skipped.
        raise InfraRepoRequiredError(
            f"Auto-created GitOps infra repo '{infra}' is not on a trusted Git host -- "
            "Assess cannot proceed without a usable GitOps infra repo."
        )
    return infra


def _assess_sync(
    repo_url: str,
    criticality: str,
    infra_repo_url: str | None = None,
    check_results_out: list[dict] | None = None,
    secret_decisions_out: list[dict] | None = None,
    secret_classify_cache: object | None = None,
):
    """Run assessment synchronously. Used by webhooks and background threads.

    GitOps registration is mandatory: resolves (and validates) a real
    infra_repo_url BEFORE cloning/running the assessment pipeline at all --
    see ``_resolve_mandatory_infra_repo_url()``. Raises
    ``InfraRepoRequiredError`` (a hard stop, no Direct Apply fallback) if none
    can be resolved, so the caller never wastes a clone+assess cycle on an
    app that can't proceed anyway.
    """
    infra = _resolve_mandatory_infra_repo_url(repo_url, infra_repo_url)
    return _clone_assess_cleanup(
        repo_url, criticality, infra,
        check_results_out=check_results_out,
        secret_decisions_out=secret_decisions_out,
        secret_classify_cache=secret_classify_cache,
    )


def _auto_create_infra_repo(repo_url: str) -> str | None:
    """Auto-create (or reuse) the shared default GitOps infra repo for the
    "no repo supplied" path.

    Passes ``repo_url``'s owner through to ``ensure_infra_repo()``, but
    that owner is NOT actually where the repo ends up living in the common
    case -- see ``ensure_infra_repo()``'s docstring: its first creation
    attempt (``/user/repos``) always lands under the authenticated token's
    own account, confirmed live to be the real, deliberate "one shared
    infra repo per token account" behavior every app in this single-tenant
    fleet already relies on today. Left unchanged; this docstring merely
    stops implying "per owner" when it's really "per token account".
    """
    try:
        from agentit.portal.github_pr import _parse_owner_repo, ensure_infra_repo
        owner, _ = _parse_owner_repo(repo_url)
        result = ensure_infra_repo(owner)
        if "repo_url" in result:
            log.info("Infra repo: %s (created=%s)", result["repo_url"], result.get("created", False))
            return result["repo_url"]
        log.warning("Failed to create infra repo: %s", result.get("error"))
    except Exception as exc:
        log.warning("Auto-create infra repo failed: %s", exc)
    return None


async def start_assess_job(
    request: Request,
    store: object,
    repo_url: str,
    criticality: str,
    infra: str | None,
    chain: bool,
) -> str:
    """Creates the assessment job, then spawns the background thread that
    runs the real clone+assess pipeline and (once complete) chains into
    onboarding -- the whole body of what used to be ``assess_submit()``'s
    own background-thread setup, moved here so that route is left with
    only Form parsing, the early trusted-host check, and building the
    redirect response. See this module's own docstring for why the
    threading/event-loop-bridging semantics below are unchanged from the
    original inline version.

    The work below runs in a background thread (long clone+assess pipeline)
    via a plain `threading.Thread`, not `asyncio.to_thread` -- unlike
    `to_thread` (awaited by the caller before the request finishes), this
    thread keeps running after the redirect below is returned, so the
    request coroutine can't stick around to `await` anything on its
    behalf.

    `AssessmentStore`'s `asyncpg` connection pool is bound to the event
    loop that created it and can't be driven from a different thread's
    loop, so every store call made from this background thread is
    scheduled back onto *this* coroutine's event loop via
    `asyncio.run_coroutine_threadsafe` -- the same pattern
    `EventConsumer._persist_dead_letter` uses for the identical
    constraint. This only works as long as that loop stays alive for the
    duration of the background thread (true for the portal's real,
    persistent uvicorn event loop; a test harness that tears its loop
    down per-request must exercise this path with its own long-lived loop
    -- see tests/test_watcher_cli_postgres.py's pattern).
    """
    job_id = await store.create_assessment_job(repo_url, continue_onboard=chain)
    loop = asyncio.get_running_loop()

    def _bridge(coro):
        """Schedule a coroutine `store.<method>(...)` call onto `loop` and
        block this worker thread until it completes."""
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)

    def _run():
        try:
            _bridge(store.update_assessment_job(job_id, "cloning", "Cloning repository..."))
            _bridge(store.update_assessment_job(job_id, "assessing", "Analyzing repository..."))
            check_results: list[dict] = []
            secret_decisions: list[dict] = []
            from agentit.secret_classify_cache import BridgedSecretClassifyCache
            classify_cache = BridgedSecretClassifyCache(store, _bridge)
            report = _assess_sync(
                repo_url, criticality, infra,
                check_results_out=check_results,
                secret_decisions_out=secret_decisions,
                secret_classify_cache=classify_cache,
            )
            _bridge(store.update_assessment_job(job_id, "saving", "Saving results..."))
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="success").inc()
            assessment_id = _bridge(store.save(report))
            _bridge(store.save_check_results(assessment_id, check_results))
            from agentit.llm_decisions import build_secret_classify_events
            for ev in build_secret_classify_events(secret_decisions, report.repo_name):
                _bridge(store.log_event(**ev, correlation_id=assessment_id))
            # `_assess_sync()` now guarantees `report.infra_repo_url` is always
            # set (`_resolve_mandatory_infra_repo_url()` raises
            # `InfraRepoRequiredError` -- handled below -- rather than ever
            # returning `None`) -- there is no more silent-failure/Direct-Apply-
            # fallback case to detect and flag here.
            # Publish event on first assessment for this repo
            history = _bridge(store.list_history(report.repo_url))
            if len(history) <= 1:
                publish_event(
                    'first-assessment', report.repo_name,
                    f'First assessment — consider running: agentit learn-for {report.repo_url}',
                    {'assessment_id': assessment_id, 'score': report.overall_score},
                    correlation_id=assessment_id,
                )

            # Deterministic, server-side assess->onboard chain (2026-07-20,
            # closing root cause #1 of the Onboard/Scan button
            # investigation, PR #99): the onboard job is created HERE, in
            # the same background thread that just saved the assessment --
            # never dependent on a browser polling `GET /assess/progress`
            # afterward. Created BEFORE this assess job is marked
            # "completed" below so a poller can never observe "completed"
            # with no onboard job yet (assess_progress() below only reads
            # this, it never creates it). The actual onboarding run is then
            # scheduled (fire-and-forget, not awaited by this thread) onto
            # the same persistent event loop `_bridge()` already targets,
            # via the exact same `_run_onboarding_job()` `onboard_submit()`
            # uses -- just scheduled from a thread instead of FastAPI's
            # `BackgroundTasks`, since this thread outlives the request/
            # response cycle those are tied to.
            onboard_job_id = _bridge(store.create_remediation_job(assessment_id)) if chain else None

            _bridge(store.update_assessment_job(job_id, "completed", "Assessment complete", assessment_id=assessment_id))

            if onboard_job_id:
                # Computed lazily, only once we know the chain will actually
                # fire -- `request` is unused by every other line of this
                # function (direct-call tests, e.g. test_assess_submit_
                # postgres.py, pass `request=None` and always opt out of
                # chaining, so this must never be evaluated for them).
                base_url = _get_trusted_base_url(request)
                asyncio.run_coroutine_threadsafe(
                    _run_onboarding_job(onboard_job_id, assessment_id, base_url), loop,
                )
        except InfraRepoRequiredError as exc:
            # A hard stop, never a fallback to Direct Apply -- all apps must
            # be GitOps-registered now. Reuses the visible-failure event
            # pattern 9e036d9 introduced for the (formerly soft-warning)
            # infra-repo-creation-failed case, but no assessment was saved
            # here at all (this fires before the clone+assess pipeline ever
            # runs), so there's no assessment_id/report to correlate to or
            # show a banner on -- the failed job page itself (assess_progress.html)
            # is where this human-readable, actionable message surfaces.
            log.warning("Assess blocked for %s: %s", repo_url, exc)
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="error").inc()
            from agentit.portal.github_pr import _parse_owner_repo
            try:
                _, app_name = _parse_owner_repo(repo_url)
            except Exception:
                app_name = None
            _bridge(store.log_event(
                "portal", "infra-repo-creation-failed", app_name, "critical",
                f"Assess blocked for {repo_url}: {exc}",
            ))
            _bridge(store.update_assessment_job(job_id, "failed", str(exc)[:280]))
        except Exception as exc:
            log.exception("Assessment failed for %s", repo_url)
            from agentit.portal.metrics import assessments_total as _at
            _at.labels(criticality=criticality, status="error").inc()
            msg = str(exc)
            if "clone" in msg.lower() or "git" in msg.lower():
                msg = f"Could not clone repository. Check the URL and permissions. ({msg[:100]})"
            elif "GITHUB_TOKEN" in msg:
                msg = "GitHub integration is not configured. Contact your administrator."
            _bridge(store.update_assessment_job(job_id, "failed", msg[:200]))

    threading.Thread(target=_run, daemon=True).start()
    return job_id
