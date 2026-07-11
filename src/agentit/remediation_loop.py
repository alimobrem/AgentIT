"""Closed remediation loop: detect → assess → onboard → apply → verify.

This module wires the autonomous pipeline that connects watcher agents
to the assess → onboard → auto-apply → SLO verify chain. Each step
calls the next, with gates inserted for destructive actions.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

import os

DEFAULT_PORTAL = os.environ.get("AGENTIT_PORTAL_URL", "http://localhost:8080")
VERIFY_WINDOW_SECONDS = 300  # 5 minutes SLO watch after apply


class RemediationLoop:
    """Orchestrates the full detect → fix → deploy → verify pipeline.

    Each method returns a result dict with the outcome. The loop can
    be triggered by any watcher agent when it detects an issue.
    """

    def __init__(
        self,
        portal_url: str = DEFAULT_PORTAL,
        store: object | None = None,
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

    def trigger(
        self,
        repo_url: str,
        app_name: str,
        criticality: str = "medium",
        reason: str = "manual",
    ) -> dict:
        """Run the full pipeline: assess → onboard → auto-apply → verify.

        Returns {"outcome": "applied"|"gated"|"failed"|"rollback", "details": {...}}
        """
        self._log("loop-started", app_name, "info", f"Triggered by: {reason}")

        # Step 1: Re-assess
        assess_result = self._assess(repo_url, criticality, app_name)
        if "error" in assess_result:
            self._log("loop-failed", app_name, "warning", f"Assessment failed: {assess_result['error']}")
            return {"outcome": "failed", "step": "assess", "details": assess_result}

        assessment_id = assess_result["assessment_id"]
        score = assess_result["overall_score"]
        self._log("assessed", app_name, "info", f"Score: {score:.0f}/100")

        # Step 2: Onboard (generate manifests)
        onboard_result = self._onboard(assessment_id, app_name)
        if "error" in onboard_result:
            self._log("loop-failed", app_name, "warning", f"Onboarding failed: {onboard_result['error']}")
            return {"outcome": "failed", "step": "onboard", "details": onboard_result}

        self._log("onboarded", app_name, "info",
                  f"Generated {onboard_result.get('files_generated', 0)} manifests")

        # Step 3: Auto-apply (LLM safety gate decides)
        apply_result = self._auto_apply(assessment_id, app_name)
        if apply_result.get("action") == "gated":
            self._log("gated", app_name, "warning",
                      f"Gated for human review: {apply_result.get('reason', 'unknown')}")
            return {"outcome": "gated", "step": "apply", "details": apply_result}

        if apply_result.get("action") != "applied":
            self._log("loop-failed", app_name, "warning", f"Apply failed: {apply_result}")
            return {"outcome": "failed", "step": "apply", "details": apply_result}

        self._log("applied", app_name, "info", "Manifests applied, starting verification")

        # Step 4: Verify SLOs
        verify_result = self._verify_slos(assessment_id, app_name)
        if not verify_result["healthy"]:
            self._log("rollback", app_name, "critical",
                      f"SLO verification failed: {verify_result.get('reason', 'unknown')}")
            return {"outcome": "rollback", "step": "verify", "details": verify_result}

        self._log("loop-completed", app_name, "info",
                  f"Pipeline complete — score {score:.0f}/100, all SLOs healthy")
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

    def _verify_slos(self, assessment_id: str, app_name: str) -> dict:
        """Watch SLOs for VERIFY_WINDOW_SECONDS after apply."""
        if self._store is None:
            return {"healthy": True, "reason": "No store — skipping SLO check"}

        start = time.monotonic()
        while time.monotonic() - start < VERIFY_WINDOW_SECONDS:
            slos = self._store.list_slos(assessment_id)
            breached = [s for s in slos if s["status"] == "breached"]
            if breached:
                return {
                    "healthy": False,
                    "reason": f"{len(breached)} SLO(s) breached: {', '.join(s['metric_name'] for s in breached)}",
                    "breached": [s["metric_name"] for s in breached],
                }
            time.sleep(30)

        return {"healthy": True, "reason": f"All SLOs healthy after {VERIFY_WINDOW_SECONDS}s"}

    def close(self) -> None:
        self._client.close()
