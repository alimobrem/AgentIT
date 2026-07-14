"""Closed remediation loop: detect -> assess -> onboard -> apply -> verify.

This module wires the autonomous pipeline that connects watcher agents
to the assess -> onboard -> auto-apply -> SLO verify chain. Each step
calls the next, with gates inserted for destructive actions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agentit.portal.store import AssessmentStore

logger = logging.getLogger(__name__)

DEFAULT_PORTAL = os.environ.get("AGENTIT_PORTAL_URL", "http://localhost:8080")

# Tracks in-flight remediation jobs as asyncio Tasks. No lock needed --
# unlike the previous threading.Thread-based implementation (which could be
# entered from a real OS worker thread), start()/active_job_count() now
# only ever run on the single event loop thread, and neither does an
# `await` in the middle of mutating this dict, so there's no interleaving
# window for another coroutine to race with.
_active_jobs: dict[str, "asyncio.Task"] = {}
MAX_CONCURRENT_REMEDIATIONS = int(os.environ.get("AGENTIT_MAX_REMEDIATIONS", "3"))
VERIFY_WINDOW_SECONDS = 60  # 60s SLO watch after apply
VERIFY_POLL_INTERVAL = 5  # poll every 5s
VERIFY_MAX_POLLS = 12  # 12 * 5s = 60s


async def verify_slos(store: object | None, assessment_id: str, app_name: str) -> dict:
    """Watch SLOs for ``VERIFY_WINDOW_SECONDS`` after a delivery.

    Extracted from ``RemediationLoop._verify_slos`` (which now delegates
    here) so the unified delivery router (``portal/delivery.py``) can run
    the exact same verify tail for every delivery, not just the fully
    autonomous fleet-watcher-triggered path -- see
    docs/unified-apply-flow.md section (C), "one loop shape."
    """
    if store is None:
        return {"healthy": True, "reason": "No store -- skipping SLO check"}

    from agentit.slo_collector import collect_slo, is_breached

    for _ in range(VERIFY_MAX_POLLS):
        slos = await store.list_slos(assessment_id)
        if not slos:
            await asyncio.sleep(VERIFY_POLL_INTERVAL)
            continue

        breached = []
        for s in slos:
            # collect_slo does blocking kubernetes-client I/O -- narrowly
            # wrapped in to_thread at this call site.
            value = await asyncio.to_thread(collect_slo, s["metric_name"], app_name)
            if value is not None:
                status = "breached" if is_breached(s["metric_name"], value, s["target_value"]) else "met"
                await store.update_slo(s["id"], value, status)
                if status == "breached":
                    breached.append(s["metric_name"])

        if breached:
            return {
                "healthy": False,
                "reason": f"{len(breached)} SLO(s) breached: {', '.join(breached)}",
                "breached": breached,
            }
        await asyncio.sleep(VERIFY_POLL_INTERVAL)

    return {"healthy": True, "reason": f"All SLOs healthy after {VERIFY_WINDOW_SECONDS}s"}


async def rollback_action(app_name: str, namespace: str) -> dict:
    """Execute rollback via ``kube.rollout_undo``.

    Extracted from ``RemediationLoop._rollback`` (which now delegates here,
    adding its own store/publisher logging around the result) so the
    unified delivery router's direct-apply verification tail
    (``portal/delivery.py::verify_and_close_delivery``) can trigger the same
    rollback without duplicating the kube call.
    """
    from agentit import kube

    result = await asyncio.to_thread(kube.rollout_undo, app_name, namespace)
    if result["success"]:
        return {"outcome": "rolled_back", "details": result["message"]}
    return {"outcome": "rollback_failed", "error": result["message"]}


class RemediationLoop:
    """Orchestrates the full detect -> fix -> deploy -> verify pipeline.

    Each method returns a result dict with the outcome. The loop can
    be triggered by any watcher agent when it detects an issue.

    ``store`` must be an async-compatible store (``AsyncSQLiteStore`` or
    ``store_pg.AssessmentStore``) -- every store call below is ``await``ed.
    """

    def __init__(
        self,
        portal_url: str = DEFAULT_PORTAL,
        store: "AssessmentStore | None" = None,
        publisher: object | None = None,
        timeout: int = 120,
    ):
        self._portal = portal_url.rstrip("/")
        self._store = store
        self._publisher = publisher
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _log(self, action: str, target: str, severity: str, summary: str) -> None:
        logger.info("[loop] %s %s: %s", action, target, summary)
        if self._store is not None:
            try:
                await self._store.log_event("remediation-loop", action, target, severity, summary)
            except Exception:
                logger.warning("Failed to log event to store", exc_info=True)
        if self._publisher is not None:
            try:
                self._publisher.publish(
                    "agentit-events",
                    agent_id="remediation-loop",
                    action=action,
                    target_app=target,
                    severity=severity,
                    summary=summary,
                )
            except Exception:
                logger.warning("Failed to publish event to Kafka", exc_info=True)

    async def start(
        self,
        repo_url: str,
        app_name: str,
        criticality: str = "medium",
        reason: str = "manual",
        store: object | None = None,
    ) -> str:
        """Schedule trigger() as a background asyncio Task, return job_id immediately.

        Previously spawned a daemon ``threading.Thread`` so the caller (a
        synchronous route) wouldn't block. Now that ``trigger()`` itself is
        a coroutine, a plain ``asyncio.create_task`` gives the same
        fire-and-forget behavior without a second OS thread -- the task
        runs on this process's single event loop, interleaved with every
        other request, exactly like any other in-flight coroutine.
        """
        job_store = store or self._store
        if job_store is None:
            raise RuntimeError("store is required for async job tracking")

        # Clean up finished tasks.
        for jid, t in list(_active_jobs.items()):
            if t.done():
                del _active_jobs[jid]

        if len(_active_jobs) >= MAX_CONCURRENT_REMEDIATIONS:
            raise RuntimeError("Max concurrent remediations reached")

        job_id = await job_store.create_remediation_job(assessment_id="")
        task = asyncio.create_task(
            self._run_with_job_tracking(job_id, repo_url, app_name, criticality, reason, job_store)
        )
        _active_jobs[job_id] = task
        return job_id

    async def _run_with_job_tracking(
        self,
        job_id: str,
        repo_url: str,
        app_name: str,
        criticality: str,
        reason: str,
        job_store: object,
    ) -> None:
        """Wrapper around trigger() that updates job status at each step."""
        try:
            result = await self.trigger(
                repo_url, app_name, criticality, reason, job_id=job_id, job_store=job_store,
            )
            if result["outcome"] in ("applied",):
                await job_store.update_remediation_job(job_id, "completed", current_step="completed")
            elif result["outcome"] == "gated":
                await job_store.update_remediation_job(job_id, "gated", current_step="gated")
            # failed/rollback already handled inside trigger()
        except Exception:
            logger.exception("Remediation job %s crashed", job_id)
            await job_store.update_remediation_job(job_id, "failed", error="unhandled exception in loop thread")

    async def trigger(
        self,
        repo_url: str,
        app_name: str,
        criticality: str = "medium",
        reason: str = "manual",
        job_id: str | None = None,
        job_store: object | None = None,
    ) -> dict:
        """Run the full pipeline: assess -> onboard -> auto-apply -> verify.

        Returns {"outcome": "applied"|"gated"|"failed"|"rollback", "details": {...}}
        """
        await self._log("loop-started", app_name, "info", f"Triggered by: {reason}")

        async def _update_job(status: str, step: str = "", error: str = "") -> None:
            if job_id and job_store is not None:
                await job_store.update_remediation_job(job_id, status, current_step=step, error=error)

        # Step 1: Re-assess
        await _update_job("assessing", "assessing")
        assess_result = await self._assess(repo_url, criticality, app_name)
        if "error" in assess_result:
            await self._log("loop-failed", app_name, "warning", f"Assessment failed: {assess_result['error']}")
            await _update_job("failed", "assessing", error=assess_result["error"])
            return {"outcome": "failed", "step": "assess", "details": assess_result}

        assessment_id = assess_result["assessment_id"]
        score = assess_result["overall_score"]
        await self._log("assessed", app_name, "info", f"Score: {score:.0f}/100")

        # Step 2: Onboard (generate manifests)
        await _update_job("onboarding", "onboarding")
        onboard_result = await self._onboard(assessment_id, app_name)
        if "error" in onboard_result:
            await self._log("loop-failed", app_name, "warning", f"Onboarding failed: {onboard_result['error']}")
            await _update_job("failed", "onboarding", error=onboard_result["error"])
            return {"outcome": "failed", "step": "onboard", "details": onboard_result}

        await self._log("onboarded", app_name, "info",
                        f"Generated {onboard_result.get('files_generated', 0)} manifests")

        # Step 3: Auto-apply (LLM safety gate decides)
        await _update_job("applying", "applying")
        apply_result = await self._auto_apply(assessment_id, app_name)
        if apply_result.get("action") == "gated":
            await self._log("gated", app_name, "warning",
                            f"Gated for human review: {apply_result.get('reason', 'unknown')}")
            await _update_job("gated", "applying")
            return {"outcome": "gated", "step": "apply", "details": apply_result}

        if apply_result.get("action") != "applied":
            await self._log("loop-failed", app_name, "warning", f"Apply failed: {apply_result}")
            await _update_job("failed", "applying", error=str(apply_result))
            return {"outcome": "failed", "step": "apply", "details": apply_result}

        await self._log("applied", app_name, "info", "Manifests applied, starting verification")

        # Step 4: Verify SLOs
        await _update_job("verifying_slos", "verifying_slos")
        verify_result = await self._verify_slos(assessment_id, app_name)
        if not verify_result["healthy"]:
            await self._log("rollback", app_name, "critical",
                            f"SLO verification failed: {verify_result.get('reason', 'unknown')}")
            rollback_result = await self._rollback(app_name, app_name)
            verify_result["rollback"] = rollback_result
            await _update_job("failed", "verifying_slos", error=verify_result.get("reason", "SLO breach"))
            return {"outcome": rollback_result["outcome"], "step": "verify", "details": verify_result}

        await self._log("loop-completed", app_name, "info",
                        f"Pipeline complete -- score {score:.0f}/100, all SLOs healthy")
        await _update_job("completed", "completed")
        return {"outcome": "applied", "step": "complete", "details": {
            "assessment_id": assessment_id,
            "score": score,
        }}

    async def _assess(self, repo_url: str, criticality: str, app_name: str) -> dict:
        try:
            resp = await self._client.post(
                f"{self._portal}/api/webhook/assess",
                json={"repo_url": repo_url, "criticality": criticality},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    async def _onboard(self, assessment_id: str, app_name: str) -> dict:
        try:
            resp = await self._client.post(
                f"{self._portal}/api/webhook/onboard",
                json={"correlationId": assessment_id},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    async def _auto_apply(self, assessment_id: str, app_name: str) -> dict:
        try:
            resp = await self._client.post(
                f"{self._portal}/api/webhook/auto-apply",
                json={"assessment_id": assessment_id, "namespace": app_name},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    async def _rollback(self, app_name: str, namespace: str) -> dict:
        """Execute rollback via kubernetes client (delegates to the shared
        ``rollback_action()``, adding this loop's own store/publisher log)."""
        result = await rollback_action(app_name, namespace)
        if result["outcome"] == "rolled_back":
            logger.info("Rollback succeeded for %s/%s: %s", namespace, app_name, result["details"])
            await self._log("rolled-back", app_name, "critical",
                            f"Rollback executed: {result['details']}")
        else:
            logger.error("Rollback failed for %s/%s: %s", namespace, app_name, result["error"])
            await self._log("rollback-failed", app_name, "critical", f"Rollback failed: {result['error']}")
        return result

    async def _verify_slos(self, assessment_id: str, app_name: str) -> dict:
        """Watch SLOs for VERIFY_WINDOW_SECONDS after apply (delegates to the
        shared ``verify_slos()``)."""
        return await verify_slos(self._store, assessment_id, app_name)

    async def close(self) -> None:
        await self._client.aclose()


def active_job_count() -> int:
    """Return the number of currently running remediation jobs."""
    for jid, t in list(_active_jobs.items()):
        if t.done():
            del _active_jobs[jid]
    return len(_active_jobs)
