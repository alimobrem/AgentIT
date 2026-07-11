"""Closed remediation loop: detect -> assess -> onboard -> apply -> verify.

This module wires the autonomous pipeline that connects watcher agents
to the assess -> onboard -> auto-apply -> SLO verify chain. Each step
calls the next, with gates inserted for destructive actions.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agentit.portal.store import AssessmentStore

logger = logging.getLogger(__name__)

DEFAULT_PORTAL = os.environ.get("AGENTIT_PORTAL_URL", "http://localhost:8080")
VERIFY_WINDOW_SECONDS = 60  # 60s SLO watch after apply
VERIFY_POLL_INTERVAL = 5  # poll every 5s
VERIFY_MAX_POLLS = 12  # 12 * 5s = 60s


class RemediationLoop:
    """Orchestrates the full detect -> fix -> deploy -> verify pipeline.

    Each method returns a result dict with the outcome. The loop can
    be triggered by any watcher agent when it detects an issue.
    """

    def __init__(
        self,
        portal_url: str = DEFAULT_PORTAL,
        store: AssessmentStore | None = None,
        publisher: object | None = None,
        timeout: int = 120,
    ):
        self._portal = portal_url.rstrip("/")
        self._store = store
        self._publisher = publisher
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _log(self, action: str, target: str, severity: str, summary: str) -> None:
        logger.info("[loop] %s %s: %s", action, target, summary)
        if self._store is not None:
            try:
                self._store.log_event("remediation-loop", action, target, severity, summary)
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

    def start(
        self,
        repo_url: str,
        app_name: str,
        criticality: str = "medium",
        reason: str = "manual",
        store: AssessmentStore | None = None,
    ) -> str:
        """Spawn trigger() in a background thread, return job_id immediately."""
        job_store = store or self._store
        if job_store is None:
            raise RuntimeError("store is required for async job tracking")

        job_id = job_store.create_remediation_job(assessment_id="")
        thread = threading.Thread(
            target=self._run_with_job_tracking,
            args=(job_id, repo_url, app_name, criticality, reason, job_store),
            daemon=True,
        )
        thread.start()
        return job_id

    def _run_with_job_tracking(
        self,
        job_id: str,
        repo_url: str,
        app_name: str,
        criticality: str,
        reason: str,
        job_store: AssessmentStore,
    ) -> None:
        """Wrapper around trigger() that updates job status at each step."""
        try:
            result = self.trigger(
                repo_url, app_name, criticality, reason, job_id=job_id, job_store=job_store,
            )
            if result["outcome"] in ("applied",):
                job_store.update_remediation_job(job_id, "completed", current_step="completed")
            elif result["outcome"] == "gated":
                job_store.update_remediation_job(job_id, "gated", current_step="gated")
            # failed/rollback already handled inside trigger()
        except Exception:
            logger.exception("Remediation job %s crashed", job_id)
            job_store.update_remediation_job(job_id, "failed", error="unhandled exception in loop thread")

    def trigger(
        self,
        repo_url: str,
        app_name: str,
        criticality: str = "medium",
        reason: str = "manual",
        job_id: str | None = None,
        job_store: AssessmentStore | None = None,
    ) -> dict:
        """Run the full pipeline: assess -> onboard -> auto-apply -> verify.

        Returns {"outcome": "applied"|"gated"|"failed"|"rollback", "details": {...}}
        """
        self._log("loop-started", app_name, "info", f"Triggered by: {reason}")

        def _update_job(status: str, step: str = "", error: str = "") -> None:
            if job_id and job_store is not None:
                job_store.update_remediation_job(job_id, status, current_step=step, error=error)

        # Step 1: Re-assess
        _update_job("assessing", "assessing")
        assess_result = self._assess(repo_url, criticality, app_name)
        if "error" in assess_result:
            self._log("loop-failed", app_name, "warning", f"Assessment failed: {assess_result['error']}")
            _update_job("failed", "assessing", error=assess_result["error"])
            return {"outcome": "failed", "step": "assess", "details": assess_result}

        assessment_id = assess_result["assessment_id"]
        score = assess_result["overall_score"]
        self._log("assessed", app_name, "info", f"Score: {score:.0f}/100")

        # Step 2: Onboard (generate manifests)
        _update_job("onboarding", "onboarding")
        onboard_result = self._onboard(assessment_id, app_name)
        if "error" in onboard_result:
            self._log("loop-failed", app_name, "warning", f"Onboarding failed: {onboard_result['error']}")
            _update_job("failed", "onboarding", error=onboard_result["error"])
            return {"outcome": "failed", "step": "onboard", "details": onboard_result}

        self._log("onboarded", app_name, "info",
                  f"Generated {onboard_result.get('files_generated', 0)} manifests")

        # Step 3: Auto-apply (LLM safety gate decides)
        _update_job("applying", "applying")
        apply_result = self._auto_apply(assessment_id, app_name)
        if apply_result.get("action") == "gated":
            self._log("gated", app_name, "warning",
                      f"Gated for human review: {apply_result.get('reason', 'unknown')}")
            _update_job("gated", "applying")
            return {"outcome": "gated", "step": "apply", "details": apply_result}

        if apply_result.get("action") != "applied":
            self._log("loop-failed", app_name, "warning", f"Apply failed: {apply_result}")
            _update_job("failed", "applying", error=str(apply_result))
            return {"outcome": "failed", "step": "apply", "details": apply_result}

        self._log("applied", app_name, "info", "Manifests applied, starting verification")

        # Step 4: Verify SLOs
        _update_job("verifying_slos", "verifying_slos")
        verify_result = self._verify_slos(assessment_id, app_name)
        if not verify_result["healthy"]:
            self._log("rollback", app_name, "critical",
                      f"SLO verification failed: {verify_result.get('reason', 'unknown')}")
            rollback_result = self._rollback(app_name, app_name)
            verify_result["rollback"] = rollback_result
            _update_job("failed", "verifying_slos", error=verify_result.get("reason", "SLO breach"))
            return {"outcome": rollback_result["outcome"], "step": "verify", "details": verify_result}

        self._log("loop-completed", app_name, "info",
                  f"Pipeline complete -- score {score:.0f}/100, all SLOs healthy")
        _update_job("completed", "completed")
        return {"outcome": "applied", "step": "complete", "details": {
            "assessment_id": assessment_id,
            "score": score,
        }}

    def _assess(self, repo_url: str, criticality: str, app_name: str) -> dict:
        try:
            resp = self._client.post(
                f"{self._portal}/api/webhook/assess",
                json={"repo_url": repo_url, "criticality": criticality},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    def _onboard(self, assessment_id: str, app_name: str) -> dict:
        try:
            resp = self._client.post(
                f"{self._portal}/api/webhook/onboard",
                json={"correlationId": assessment_id},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    def _auto_apply(self, assessment_id: str, app_name: str) -> dict:
        try:
            resp = self._client.post(
                f"{self._portal}/api/webhook/auto-apply",
                json={"assessment_id": assessment_id, "namespace": app_name},
            )
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return resp.json()
        except Exception as exc:
            return {"error": str(exc)}

    def _rollback(self, app_name: str, namespace: str) -> dict:
        """Execute rollback via oc rollout undo."""
        cli = shutil.which("oc") or shutil.which("kubectl")
        if cli is None:
            logger.error("Neither oc nor kubectl found, cannot rollback %s", app_name)
            return {"outcome": "rollback_failed", "error": "no CLI tool found"}

        try:
            result = subprocess.run(
                [cli, "rollout", "undo", f"deployment/{app_name}", "-n", namespace],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.exception("Rollback timed out for %s/%s", namespace, app_name)
            self._log("rollback-failed", app_name, "critical", "Rollback timed out")
            return {"outcome": "rollback_failed", "error": "timeout"}

        if result.returncode == 0:
            detail = result.stdout.strip()
            logger.info("Rollback succeeded for %s/%s: %s", namespace, app_name, detail)
            self._log("rolled-back", app_name, "critical",
                      f"Rollback executed: {detail}")
            return {"outcome": "rolled_back", "details": detail}

        err = result.stderr.strip()
        logger.error("Rollback failed for %s/%s: %s", namespace, app_name, err)
        self._log("rollback-failed", app_name, "critical", f"Rollback failed: {err}")
        return {"outcome": "rollback_failed", "error": err}

    def _verify_slos(self, assessment_id: str, app_name: str) -> dict:
        """Watch SLOs for VERIFY_WINDOW_SECONDS after apply."""
        if self._store is None:
            return {"healthy": True, "reason": "No store -- skipping SLO check"}

        from agentit.slo_collector import collect_slo

        for _ in range(VERIFY_MAX_POLLS):
            slos = self._store.list_slos(assessment_id)
            if not slos:
                time.sleep(VERIFY_POLL_INTERVAL)
                continue

            breached = []
            for s in slos:
                value = collect_slo(s["metric_name"], app_name)
                if value is not None:
                    status = "breached" if value > s["target_value"] else "met"
                    self._store.update_slo(s["id"], value, status)
                    if status == "breached":
                        breached.append(s["metric_name"])

            if breached:
                return {
                    "healthy": False,
                    "reason": f"{len(breached)} SLO(s) breached: {', '.join(breached)}",
                    "breached": breached,
                }
            time.sleep(VERIFY_POLL_INTERVAL)

        return {"healthy": True, "reason": f"All SLOs healthy after {VERIFY_WINDOW_SECONDS}s"}

    def close(self) -> None:
        self._client.close()
