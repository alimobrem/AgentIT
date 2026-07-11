"""Auto-mode decision engine — determines whether to auto-apply or gate for human review."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.8


class AutoMode:
    """Decides whether agent-generated manifests should be auto-applied or queued.

    Decision matrix:
      auto_mode OFF → always gate (current behavior)
      auto_mode ON + orchestrator auto_approve + LLM says safe → auto-apply
      auto_mode ON + orchestrator auto_approve + LLM says destructive → gate + alert
      auto_mode ON + orchestrator says no auto_approve → gate
      auto_mode ON + LLM unavailable → gate (fail-closed)
    """

    def __init__(self, store: object, publisher: object | None = None, llm_client: object | None = None):
        self._store = store
        self._publisher = publisher
        self._llm = llm_client

    @property
    def enabled(self) -> bool:
        env = os.environ.get("AGENTIT_AUTO_MODE", "").lower()
        if env in ("1", "true", "on"):
            return True
        try:
            val = self._store.get_setting("auto_mode")
            return val in ("1", "true", "on")
        except Exception:
            return False

    def should_auto_apply(
        self,
        auto_approve: bool,
        manifests: list[str],
        criticality: str,
        app_name: str,
    ) -> tuple[bool, str]:
        """Returns (can_auto_apply, reason)."""
        if not self.enabled:
            return False, "auto-mode is disabled"

        if not auto_approve:
            return False, f"orchestrator requires human approval (criticality={criticality})"

        if self._llm is None:
            return False, "LLM unavailable — fail-closed, requiring human approval"

        classification = self._llm.classify_action(
            action_type="apply",
            manifests=manifests,
            context=f"App: {app_name}, Criticality: {criticality}",
        )

        if classification is None:
            return False, "LLM classification failed — fail-closed, requiring human approval"

        if classification["confidence"] < _CONFIDENCE_THRESHOLD:
            return False, f"LLM confidence too low ({classification['confidence']:.2f}) — requiring human review"

        if classification["is_destructive"]:
            return False, f"LLM flagged as destructive: {classification['reason']}"

        return True, f"LLM classified as safe ({classification['confidence']:.2f}): {classification['reason']}"

    def execute(
        self,
        assessment_id: str,
        files: list[dict],
        namespace: str,
        criticality: str,
        auto_approve: bool,
        app_name: str,
    ) -> dict:
        """Full auto-apply pipeline: dry-run → classify → apply or gate.

        Returns {"action": "applied"|"gated"|"failed", "reason": str, "details": dict}
        """
        from agentit.portal.cluster_apply import apply_manifests_to_cluster

        manifests = [f["content"] for f in files if f["path"].endswith((".yaml", ".yml"))]

        can_apply, reason = self.should_auto_apply(auto_approve, manifests, criticality, app_name)

        self._log_event(
            "auto-mode",
            "decision",
            app_name,
            "info" if can_apply else "warning",
            f"{'AUTO-APPLY' if can_apply else 'GATE'}: {reason}",
        )

        if not can_apply:
            gate_id = self._store.create_gate(
                assessment_id, "auto-mode-review",
                f"Auto-mode gated: {reason}",
            )
            return {"action": "gated", "reason": reason, "details": {"gate_id": gate_id}}

        # Step 1: dry-run
        dry_result = apply_manifests_to_cluster(files, namespace, dry_run=True)
        if dry_result["errors"]:
            self._log_event(
                "auto-mode", "dry-run-failed", app_name, "warning",
                f"Dry-run failed with {len(dry_result['errors'])} error(s)",
            )
            gate_id = self._store.create_gate(
                assessment_id, "dry-run-failed",
                f"Dry-run failed: {'; '.join(dry_result['errors'][:3])}",
            )
            return {"action": "gated", "reason": "dry-run failed", "details": dry_result}

        # Step 2: apply for real
        result = apply_manifests_to_cluster(files, namespace, dry_run=False)

        self._log_event(
            "auto-mode", "auto-applied", app_name, "info",
            f"Applied {len(result['applied'])} manifests to {namespace}",
        )

        # Mark remediations as complete
        remediations = self._store.list_remediations(assessment_id)
        for rem in remediations:
            if rem["status"] != "completed":
                self._store.complete_remediation(rem["id"])

        return {
            "action": "applied",
            "reason": reason,
            "details": result,
        }

    def _log_event(self, agent_id: str, action: str, target: str, severity: str, summary: str) -> None:
        try:
            self._store.log_event(agent_id, action, target, severity, summary)
        except Exception as exc:
            logger.warning("Failed to log auto-mode event: %s", exc)

        if self._publisher is not None:
            try:
                self._publisher.publish(
                    "agentit-events",
                    agent_id=agent_id,
                    action=action,
                    target_app=target,
                    severity=severity,
                    summary=summary,
                )
            except Exception as exc:
                logger.warning("Failed to publish auto-mode event: %s", exc)
