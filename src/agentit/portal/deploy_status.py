"""Deploy-status computation: "is a deploy in progress, and what's
changing" for AgentIT's own instance -- the currently-running build
(``agentit_build`` Info metric), the live ``agentit-ci`` Tekton
``PipelineRun``, and the live ``agentit`` Argo CD ``Application``, plus the
commit-comparison logic that decides whether the two agree.

Pure extraction from ``routes/health.py`` (which mixed this self-contained
block with unrelated HTTP route handlers) -- ``routes/health.py``'s
``GET /health`` and ``GET /api/deploy-status`` routes import and call into
this module, keeping only the actual route handlers and their thin
response-building glue. Same function signatures/return shapes as before
the move; no behavior change.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time

from agentit import kube
from agentit.portal import github_pr

logger = logging.getLogger(__name__)

# Ambient /api/deploy-status is polled every 15s from every page. Keep kube
# calls short and cache last-good so a wedged apiserver cannot pin workers
# until oauth-proxy returns 502/503 awaiting response headers.
_DEPLOY_STATUS_K8S_TIMEOUT = 2
_DEPLOY_STATUS_DEADLINE = 3.0
_DEPLOY_STATUS_CACHE_TTL = 20.0
_deploy_status_cache: dict = {"data": None, "ts": 0.0}
# `asyncio.wait_for(asyncio.to_thread(...), timeout=...)` below only
# cancels the *awaiting* coroutine on timeout -- the dispatched thread
# itself keeps running in whichever executor it was submitted to
# regardless (a `concurrent.futures` work item can't be interrupted once
# started). Using the process-wide default executor (what `asyncio.
# to_thread` submits to) would mean a genuinely wedged apiserver leaks one
# stuck thread per poll (every 15s, from every open page) into a pool
# shared with every other `asyncio.to_thread` call in the app, eventually
# starving unrelated work too. A small, dedicated executor instead
# contains that blast radius to this one route: worst case, these 4
# workers back up, but nothing outside this endpoint is affected --
# `loop.run_in_executor(_deploy_status_executor, ...)` here isn't itself
# a full fix (the underlying kube call can still hang past the deadline;
# see the deferred follow-up noted alongside this route below), just a
# bounded-blast-radius mitigation for the exhaustion risk specifically.
_deploy_status_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="deploy-status",
)
# `_get_deploy_status_bounded` runs in a real OS thread (via
# `asyncio.to_thread`), so concurrent pollers can genuinely interleave the
# read-check-write below at the bytecode level -- an `asyncio.Lock` would
# not help here since it only excludes other coroutines on the same event
# loop thread, not other OS threads.
_deploy_status_cache_lock = threading.Lock()


def _taskrun_status(name: str, namespace: str = "agentit") -> str:
    """Real status for a single Tekton TaskRun by name.

    Verified live against a real cluster: Tekton v1 ``PipelineRun.status.
    childReferences`` entries carry only ``name``/``kind``/``pipelineTaskName``
    -- never an embedded ``conditions`` array (that was a v1beta1-only field,
    since removed) -- so a task's actual running/done/failed state has to be
    read off its own TaskRun object, one ``get`` per child. Without this, a
    naive read of ``childReferences[].conditions`` (as ``pipeline_detail_page``
    above still does) always reports "Unknown" for every task on this
    cluster's Tekton version, regardless of real progress.
    """
    try:
        tr = kube.get_custom_resource(
            "tekton.dev", "v1", "taskruns", name,
            namespace=namespace, timeout=_DEPLOY_STATUS_K8S_TIMEOUT,
        )
        if tr is None:
            return "Pending"
        conditions = tr.get("status", {}).get("conditions", [{}])
        return conditions[0].get("reason", "Unknown") if conditions else "Unknown"
    except Exception:
        logger.debug("Failed to read TaskRun %s for deploy status", name, exc_info=True)
        return "Unknown"


def _degraded_deploy_status(errors: list[str] | None = None) -> dict:
    """Fallback when kube calls time out or fail -- never hang the badge."""
    from agentit.portal.metrics import get_build_info

    return {
        "running": get_build_info(),
        "pipeline": None,
        "argo": None,
        "commit_info": None,
        "state": "idle",
        "stage": None,
        "reason": None,
        "resolved": None,
        "errors": list(errors or ["Deploy status unavailable"]),
        "degraded": True,
    }


def _get_fresh_cached_deploy_status() -> dict | None:
    with _deploy_status_cache_lock:
        cached = _deploy_status_cache["data"]
        if cached is None:
            return None
        if (time.monotonic() - _deploy_status_cache["ts"]) >= _DEPLOY_STATUS_CACHE_TTL:
            return None
        return cached


def _get_last_good_deploy_status() -> dict | None:
    with _deploy_status_cache_lock:
        return _deploy_status_cache["data"]


def _store_deploy_status_cache(status: dict) -> None:
    """Cache last-good only (no errors) so degraded polls can fall back."""
    if status.get("errors") or status.get("degraded"):
        return
    with _deploy_status_cache_lock:
        _deploy_status_cache["data"] = status
        _deploy_status_cache["ts"] = time.monotonic()


def _get_deploy_status_bounded(include_commit_info: bool = False) -> dict:
    """Cache-aware deploy status for the ambient badge (no GitHub enrichment).

    Serves a fresh cache hit within ``_DEPLOY_STATUS_CACHE_TTL`` to cut
    apiserver load from htmx polling; on API errors prefers last-good over
    a blank degraded response when available.
    """
    if not include_commit_info:
        cached = _get_fresh_cached_deploy_status()
        if cached is not None:
            return cached

    status = _get_deploy_status(include_commit_info=include_commit_info)

    if not include_commit_info:
        if not status.get("errors"):
            _store_deploy_status_cache(status)
        else:
            last_good = _get_last_good_deploy_status()
            if last_good is not None:
                out = dict(last_good)
                out["errors"] = list(status.get("errors") or []) + [
                    "Serving last-good deploy status",
                ]
                out["degraded"] = True
                return out
    return status


# Tekton reasons meaning the PipelineRun controller couldn't even resolve
# the Pipeline/Task definitions -- verified live (2026-07-18 etcd-timeout
# incident): these terminate with an empty/absent `childReferences`, i.e.
# not a single TaskRun was ever created, so the run never reached
# build-image/image-push and cannot have affected what's actually live.
_TASK_RESOLUTION_FAILURE_REASONS = ("CouldntGetTask", "CouldntGetPipeline")


def _is_confirmed_unreleased_revision(pipeline_revision: str, running_commit: str) -> bool:
    """True only when both commits are known and *provably* different.

    Deliberately fails closed: an "unknown" `running_commit` (build info not
    yet populated) or a missing `pipeline_revision` must never be treated as
    "different" -- that would let an ambiguous read excuse a real failure.
    Reuses the same startswith-either-direction comparison already used
    below for the `resolved` healthy/rolled_back check, since one side is
    sometimes a short hash.
    """
    if not pipeline_revision or running_commit in ("unknown", ""):
        return False
    return not (
        running_commit == pipeline_revision
        or running_commit.startswith(pipeline_revision)
        or pipeline_revision.startswith(running_commit)
    )


def _get_deploy_status(include_commit_info: bool = False) -> dict:
    """"Is a deploy in progress, and what's changing" -- combines the
    currently-running build (`agentit_build` Info metric, via
    ``get_build_info()``) with the live ``agentit-ci`` PipelineRun and the
    live ``agentit`` Argo CD Application. Backs both the ambient nav badge
    (``/api/deploy-status``, ``include_commit_info=False`` -- no GitHub call
    on every poll) and the Health page's detailed section
    (``include_commit_info=True``).

    Every field is either real data from a live API call or ``None``/absent
    on failure -- an unreachable PipelineRun/Application API is reported via
    ``errors``, never silently treated as "nothing in progress".
    """
    from agentit.portal.metrics import get_build_info

    status: dict = {
        "running": get_build_info(),
        "pipeline": None,
        "argo": None,
        "commit_info": None,
        "state": "idle",  # "idle" | "deploying" | "failed"
        "stage": None,
        "reason": None,
        "resolved": None,
        "unreleased_ci_failure": None,
        "errors": [],
    }

    latest_run = None
    try:
        runs = kube.list_custom_resources(
            "tekton.dev", "v1", "pipelineruns",
            namespace="agentit", timeout=_DEPLOY_STATUS_K8S_TIMEOUT,
        )
        ci_runs = [
            r for r in runs
            if r.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline") == "agentit-ci"
        ]
        # list_custom_resources order is not guaranteed -- pick newest by time.
        ci_runs.sort(key=lambda r: r.get("metadata", {}).get("creationTimestamp") or "")
        latest_run = ci_runs[-1] if ci_runs else None
    except Exception:
        logger.warning("Failed to list agentit-ci PipelineRuns for deploy status", exc_info=True)
        status["errors"].append("Could not reach the Tekton PipelineRun API")

    if latest_run is not None:
        conditions = latest_run.get("status", {}).get("conditions", [{}])
        cond = conditions[0] if conditions else {}
        reason = cond.get("reason", "Unknown")
        is_running = cond.get("status", "Unknown") == "Unknown"
        revision = next(
            (p.get("value", "") for p in latest_run.get("spec", {}).get("params", []) if p.get("name") == "revision"),
            "",
        )
        tasks = []
        for child in latest_run.get("status", {}).get("childReferences", []):
            child_name = child.get("name", "")
            task_reason = _taskrun_status(child_name) if child_name else "Unknown"
            tasks.append({"name": child.get("pipelineTaskName", child_name or "?"), "status": task_reason})
        status["pipeline"] = {
            "name": latest_run.get("metadata", {}).get("name", "?"),
            "reason": reason,
            "running": is_running,
            "revision": revision,
            "tasks": tasks,
        }
        if is_running:
            status["state"] = "deploying"
            status["stage"] = next((t["name"] for t in tasks if t["status"] != "Succeeded"), "starting")
        elif reason in ("Cancelled", "PipelineRunCancelled"):
            # Cancelled runs are operator/capacity noise (concurrent CI cancelled
            # to free the node), not a deploy failure. Surface the run but leave
            # state idle so Argo/Rollout decide the real outcome.
            pass
        elif reason in _TASK_RESOLUTION_FAILURE_REASONS and not tasks:
            # Never created a single TaskRun for this commit (confirmed by
            # `not tasks`, i.e. an empty childReferences) -- nothing was ever
            # built or pushed, so this can't be a regression in what's live.
            # Record it separately and let Argo/Rollout (checked below)
            # decide the real live state, same as the Cancelled carve-out.
            status["unreleased_ci_failure"] = {"reason": reason, "revision": revision}
        elif reason not in ("Succeeded", "Completed") and _is_confirmed_unreleased_revision(
            revision, status["running"].get("commit", "unknown"),
        ):
            # Verified live (2026-07-18, `TaskRunTimeout` on `run-tests` under
            # heavy concurrent CI load): a real task failure/timeout can occur
            # on *any* task, for *any* reason string -- an allowlist of known
            # reasons (like the two carve-outs above) will always be one
            # incident behind. The one fact that's actually true regardless of
            # reason: if this run's target `revision` provably differs from
            # the commit this very instance is running (`get_build_info()`,
            # real build-time data, not a guess), that commit was never
            # deployed here, so its build/test failure cannot be a live
            # regression. Only trust this when *both* commits are known and
            # unambiguous -- an "unknown" build-info commit must not be used
            # to excuse a real failure (see `_is_confirmed_unreleased_revision`).
            status["unreleased_ci_failure"] = {"reason": reason, "revision": revision}
        elif reason not in ("Succeeded", "Completed"):
            status["state"] = "failed"
            status["reason"] = f"CI pipeline {reason}"

    agentit_app = None
    try:
        apps = kube.list_custom_resources(
            "argoproj.io", "v1alpha1", "applications",
            namespace="openshift-gitops", timeout=_DEPLOY_STATUS_K8S_TIMEOUT,
        )
        agentit_app = next((a for a in apps if a.get("metadata", {}).get("name") == "agentit"), None)
    except Exception:
        logger.warning("Failed to fetch the agentit Argo CD Application for deploy status", exc_info=True)
        status["errors"].append("Could not reach the Argo CD Application API")

    if agentit_app is not None:
        sync = agentit_app.get("status", {}).get("sync", {})
        health = agentit_app.get("status", {}).get("health", {})
        sync_status = sync.get("status", "Unknown")
        health_status = health.get("status", "Unknown")
        params = agentit_app.get("spec", {}).get("source", {}).get("helm", {}).get("parameters", [])
        image_tag = next((p.get("value", "") for p in params if p.get("name") == "image.tag"), "")
        op_phase = (agentit_app.get("status", {}).get("operationState") or {}).get("phase", "")
        status["argo"] = {
            "sync": sync_status,
            "health": health_status,
            "health_message": health.get("message", ""),
            "image_tag": image_tag,
            "repo_url": agentit_app.get("spec", {}).get("source", {}).get("repoURL", ""),
            "operation_phase": op_phase,
        }
        if status["state"] != "deploying":
            # Active sync/hook work is "deploying", not Failed -- Degraded is
            # often transient (PDB SyncFailed while a Job matched the selector,
            # or a Sync hook Pending) while operationState is still Running.
            if op_phase == "Running" or health_status in ("Progressing", "Suspended"):
                # Suspended = canary pause step (Rollout), still an in-flight deploy.
                status["state"] = "deploying"
                if op_phase == "Running":
                    status["stage"] = "syncing"
                elif health_status == "Suspended":
                    status["stage"] = "canary pause"
                else:
                    status["stage"] = "rolling out"
            elif health_status == "Degraded":
                status["state"] = "failed"
                status["reason"] = status["argo"]["health_message"] or "Argo CD reports the agentit Application as Degraded"
            elif sync_status != "Synced":
                status["state"] = "deploying"
                status["stage"] = "syncing"

    # Only worth surfacing the unreleased-CI-build note when the live
    # deploy really is idle/healthy -- if Argo/Rollout turned out Degraded
    # for a genuinely unrelated reason, that real failure should be the
    # one message shown, not a distraction alongside it.
    if status["state"] != "idle":
        status["unreleased_ci_failure"] = None

    # Resolved outcome: once the latest PipelineRun succeeded and nothing is
    # actively deploying/failed, check whether this running instance's own
    # commit (its real, live env, not a guess) actually matches that
    # PipelineRun's target revision -- catches a canary that got aborted /
    # rolled back after the pipeline itself succeeded.
    if status["state"] == "idle" and status["pipeline"] and status["pipeline"]["reason"] == "Succeeded":
        running_commit = status["running"].get("commit", "unknown")
        revision = status["pipeline"]["revision"]
        if running_commit not in ("unknown", "") and revision:
            if running_commit == revision or running_commit.startswith(revision) or revision.startswith(running_commit):
                status["resolved"] = {
                    "outcome": "healthy",
                    "message": "This instance is running the version built by the latest PipelineRun.",
                }
            else:
                msg = (
                    f"This instance is running commit {running_commit[:12]}, not the latest "
                    f"PipelineRun's target ({revision[:12]}) -- likely rolled back."
                )
                if status["argo"] and status["argo"].get("health_message"):
                    msg += f" Argo CD: {status['argo']['health_message']}"
                status["resolved"] = {"outcome": "rolled_back", "message": msg}

    if include_commit_info and status["pipeline"] and status["argo"]:
        revision = status["pipeline"].get("revision")
        repo_url = status["argo"].get("repo_url")
        if revision and repo_url:
            try:
                status["commit_info"] = github_pr.get_commit_info(repo_url, revision) or None
            except Exception:
                logger.warning("Failed to fetch commit info for deploy status", exc_info=True)

    return status
