"""Auto-mode decision engine — determines whether to auto-apply or gate for human review."""

from __future__ import annotations

import asyncio
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

    ``store`` must be an async-compatible store (``store_factory.AsyncSQLiteStore``
    or ``store_pg.AssessmentStore``) — every store call below is ``await``ed.
    ``llm_client`` (``agentit.llm.LLMClient``) stays a synchronous object;
    its one blocking network call (``classify_action``) is dispatched via
    ``asyncio.to_thread`` at the call site in ``should_auto_apply`` rather
    than requiring an async LLM client, since this is the only one of
    llm.py's four methods called from an already-converted async class.
    """

    def __init__(self, store: object, publisher: object | None = None, llm_client: object | None = None):
        self._store = store
        self._publisher = publisher
        self._llm = llm_client

    async def is_enabled(self) -> bool:
        """Whether auto-mode is currently enabled (env var, then store setting).

        Was a synchronous ``enabled`` property; converted to an async method
        because the store-setting fallback now requires an ``await``. Every
        caller (``should_auto_apply`` below, watchers, webhook routes) has
        been updated to ``await auto.is_enabled()``.
        """
        env = os.environ.get("AGENTIT_AUTO_MODE", "").lower()
        if env in ("1", "true", "on"):
            return True
        try:
            val = await self._store.get_setting("auto_mode")
            return val in ("1", "true", "on")
        except Exception as exc:
            logger.warning("Failed to read auto_mode setting from store, defaulting to disabled: %s", exc)
            return False

    async def should_auto_apply(
        self,
        auto_approve: bool,
        manifests: list[str],
        criticality: str,
        app_name: str,
    ) -> tuple[bool, str]:
        """Returns (can_auto_apply, reason)."""
        if not await self.is_enabled():
            return False, "auto-mode is disabled"

        if not auto_approve:
            return False, f"orchestrator requires human approval (criticality={criticality})"

        if self._llm is None:
            return False, "LLM unavailable — fail-closed, requiring human approval"

        try:
            classification = await asyncio.to_thread(
                self._llm.classify_action,
                action_type="apply",
                manifests=manifests,
                context=f"App: {app_name}, Criticality: {criticality}",
            )
        except Exception as exc:
            logger.warning("LLM classify_action call failed: %s", exc)
            return False, "LLM classification failed — fail-closed, requiring human approval"

        if classification is None:
            return False, "LLM classification failed — fail-closed, requiring human approval"

        if classification["confidence"] < _CONFIDENCE_THRESHOLD:
            return False, f"LLM confidence too low ({classification['confidence']:.2f}) — requiring human review"

        if classification["is_destructive"]:
            return False, f"LLM flagged as destructive: {classification['reason']}"

        return True, f"LLM classified as safe ({classification['confidence']:.2f}): {classification['reason']}"

    async def execute(
        self,
        assessment_id: str,
        files: list[dict],
        namespace: str,
        criticality: str,
        auto_approve: bool,
        app_name: str,
        agent_name: str | None = None,
    ) -> dict:
        """Full auto-apply pipeline: dry-run → classify → apply or gate.

        `agent_name` is the originating agent/skill that generated `files`, when the
        caller knows it (e.g. the dispatcher's `result["agent"]`) — it's logged as the
        decision event's `agent_id` so this decision can be attributed to a real agent/
        skill instead of the generic "auto-mode" component name. Callers that don't
        know the originating agent (e.g. onboarding, which spans many agents at once)
        can omit it and the decision is logged under "auto-mode" as before.

        Returns {"action": "applied"|"gated"|"failed", "reason": str, "details": dict}
        """
        from agentit.portal.cluster_apply import apply_manifests_to_cluster

        manifests = [f["content"] for f in files if f["path"].endswith((".yaml", ".yml"))]

        can_apply, reason = await self.should_auto_apply(auto_approve, manifests, criticality, app_name)

        await self._log_event(
            agent_name or "auto-mode",
            "decision",
            app_name,
            "info" if can_apply else "warning",
            f"{'AUTO-APPLY' if can_apply else 'GATE'}: {reason}",
        )

        if not can_apply:
            gate_id = await self._store.create_gate(
                assessment_id, "auto-mode-review",
                f"Auto-mode gated: {reason}",
            )
            return {"action": "gated", "reason": reason, "details": {"gate_id": gate_id}}

        # Step 1: dry-run (kube/oc apply is a blocking, synchronous call --
        # narrowly wrapped in to_thread right here, not the whole method).
        dry_result = await asyncio.to_thread(apply_manifests_to_cluster, files, namespace, dry_run=True)
        if dry_result["errors"]:
            await self._log_event(
                "auto-mode", "dry-run-failed", app_name, "warning",
                f"Dry-run failed with {len(dry_result['errors'])} error(s)",
            )
            gate_id = await self._store.create_gate(
                assessment_id, "dry-run-failed",
                f"Dry-run failed: {'; '.join(dry_result['errors'][:3])}",
            )
            return {"action": "gated", "reason": "dry-run failed", "details": dry_result}

        # Step 2: apply for real
        result = await asyncio.to_thread(apply_manifests_to_cluster, files, namespace, dry_run=False)

        if result["errors"]:
            await self._log_event(
                "auto-mode", "partial-failure", app_name, "warning",
                f"Applied {len(result['applied'])} but {len(result['errors'])} error(s) in {namespace}",
            )
            return {
                "action": "partial_failure",
                "reason": f"partial failure: {len(result['errors'])} error(s)",
                "details": result,
            }

        await self._log_event(
            "auto-mode", "auto-applied", app_name, "info",
            f"Applied {len(result['applied'])} manifests to {namespace}",
        )

        # Per-skill outcome, in addition to the generic event above -- lets
        # skill_effectiveness see auto-mode's real-world successes, not just
        # self-fix's. Deliberately only recorded for a genuine successful
        # apply: "gated" is a deferral to a human, not a verdict on the
        # skill, and is intentionally left unrecorded here (the human's
        # eventual decision at /gates is what records approved/rejected for
        # a gated candidate).
        from agentit.skill_engine import record_skill_outcomes
        await record_skill_outcomes(
            self._store, app_name, files, set(result["applied"]), "approved",
            reason,
        )

        remediations = await self._store.list_remediations(assessment_id)
        for rem in remediations:
            if rem["status"] != "completed":
                await self._store.complete_remediation(rem["id"])

        return {
            "action": "applied",
            "reason": reason,
            "details": result,
        }

    async def _log_event(self, agent_id: str, action: str, target: str, severity: str, summary: str) -> None:
        try:
            await self._store.log_event(agent_id, action, target, severity, summary)
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
