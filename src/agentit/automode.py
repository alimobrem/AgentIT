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

    Once the matrix above approves a batch (``should_auto_apply()``'s LLM
    safety classification), ``execute()`` hands the whole batch straight to
    ``portal/delivery.py::route_and_deliver()`` -- the same decision surface
    the manual "Deliver" button and gate-approve use (see
    docs/unified-apply-flow.md) -- for classification (secret-block,
    unresolved-placeholder guard, CI/CD-shared-namespace escalation),
    GitOps-registration lookup, and mechanism selection. This class no
    longer re-decides any of that on its own; it only supplies its own one
    extra, AutoMode-specific safety layer on top (the LLM classification
    above) before handing off. Since Direct Apply has been removed as a
    concept entirely, ``AutoMode`` never mutates a cluster directly anymore
    either: its one live terminal outcome for the cluster-config category is
    the same GitOps commit+PR every other caller gets, gated on a human
    merge (``_finish_gitops_pr()``) -- it reduces to LLM-classify → GitOps
    commit+PR, nothing else, for that category. (The per-(namespace, kind)
    auto-mode allowlist that used to scope AutoMode's own direct-apply
    branch -- and the Settings UI for it -- has been removed along with that
    branch: its entire purpose was bounding what AutoMode could mutate
    *without a human already in the loop*, which no longer describes any
    outcome AutoMode can reach.)

    ``store`` is an ``AssessmentStore`` — every store call below is ``await``ed.
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

    async def should_auto_apply_and_log(
        self,
        auto_approve: bool,
        manifests: list[str],
        criticality: str,
        app_name: str,
        agent_name: str | None = None,
    ) -> tuple[bool, str]:
        """``should_auto_apply()`` plus the same ``"decision"`` event log
        every caller needs for the Decisions page (``llm_decisions.py``'s
        ``_auto_mode_decisions()`` parses this exact "AUTO-APPLY:"/"GATE:"
        summary format) -- factored out of ``execute()`` below so a caller
        that wants this safety classification without ``execute()``'s
        full auto-apply-or-gate pipeline (e.g. onboarding's own Dry Run ->
        Deliver chain, ``portal/delivery.py::auto_dry_run_then_deliver()``)
        still gets identical Decisions-page visibility, not a silent,
        unlogged classification.
        """
        can_apply, reason = await self.should_auto_apply(auto_approve, manifests, criticality, app_name)
        await self._log_event(
            agent_name or "auto-mode",
            "decision",
            app_name,
            "info" if can_apply else "warning",
            f"{'AUTO-APPLY' if can_apply else 'GATE'}: {reason}",
        )
        return can_apply, reason

    async def execute(
        self,
        assessment_id: str,
        files: list[dict],
        namespace: str,
        criticality: str,
        auto_approve: bool,
        app_name: str,
        agent_name: str | None = None,
        report: object | None = None,
        target_findings: list[tuple[str, str]] | None = None,
    ) -> dict:
        """Full auto-apply pipeline: classify → deliver or gate.

        `agent_name` is the originating agent/skill that generated `files`, when the
        caller knows it (e.g. the dispatcher's `result["agent"]`) — it's logged as the
        decision event's `agent_id` so this decision can be attributed to a real agent/
        skill instead of the generic "auto-mode" component name. Callers that don't
        know the originating agent (e.g. onboarding, which spans many agents at once)
        can omit it and the decision is logged under "auto-mode" as before.

        `report` (the assessment's `AssessmentReport`, when the caller has it -- see
        `routes/webhooks.py`'s `webhook_auto_apply`/`webhook_finding`) is threaded
        straight through to `route_and_deliver()` below, which is what actually
        decides GitOps registration and mechanism now -- `should_auto_apply()`'s
        safety classification above is completely unchanged by it. Direct Apply
        has been removed as a concept entirely: omitting `report` (or a `report`
        with no known `infra_repo_url`) can no longer fall back to a direct
        apply -- `route_and_deliver()` refuses outright (`MECHANISM_NONE`) and
        this gates for human review with a routing-error reason instead.

        `target_findings` (docs/onboarding-loop-vision-gap-analysis.md Phase
        3), when the caller knows which specific finding(s) `files` was
        generated to resolve (e.g. a single-finding webhook dispatch), is
        threaded straight through to `route_and_deliver()` so the resulting
        `deliveries` row can later be correlated against a re-assessment's
        diff. Omitted for callers spanning many findings at once (e.g.
        onboarding's whole-batch auto-chain, which threads the full finding
        list through its own call site instead).

        Returns {"action": "applied"|"partial_failure"|"gated", "reason": str, "details": dict}.
        There is no more per-(namespace, kind) allowlist scoping AutoMode
        used to apply on top of this decision (the whole batch this
        approves is handed to the router as-is now) -- see the class
        docstring for why that layer's entire purpose evaporated along with
        Direct Apply, so `"split"` is no longer a possible action either.
        """
        from agentit.portal.delivery import CATEGORY_CLUSTER_CONFIG, MECHANISM_INFRA_REPO_COMMIT, route_and_deliver

        manifests = [f["content"] for f in files if f["path"].endswith((".yaml", ".yml"))]

        can_apply, reason = await self.should_auto_apply_and_log(
            auto_approve, manifests, criticality, app_name, agent_name,
        )

        if not can_apply:
            gate_id = await self._store.create_gate(
                assessment_id, "auto-mode-review",
                f"Auto-mode gated: {reason}",
            )
            return {"action": "gated", "reason": reason, "details": {"gate_id": gate_id}}

        # Delegates to the exact same decision surface every other
        # delivery-triggering caller uses (manual Deliver, gate-approve,
        # docs/unified-apply-flow.md): classify each file (secret-block,
        # unresolved-placeholder guard, CI/CD-shared-namespace escalation
        # all apply uniformly), look up GitOps registration, and pick the
        # mechanism per classified group.
        delivery = await route_and_deliver(
            files, app_name=app_name, namespace=namespace, report=report,
            store=self._store, assessment_id=assessment_id, actor="auto-mode",
            dry_run=False, target_findings=target_findings,
        )

        mechanism_used = delivery["mechanisms"].get(CATEGORY_CLUSTER_CONFIG)
        cluster_outcome = delivery["outcomes"].get(CATEGORY_CLUSTER_CONFIG)

        if mechanism_used == MECHANISM_INFRA_REPO_COMMIT:
            return await self._finish_gitops_pr(assessment_id, app_name, reason, cluster_outcome, delivery)

        return await self._finish_without_cluster_config_delivery(
            assessment_id, app_name, reason, cluster_outcome, delivery,
        )

    async def _finish_gitops_pr(
        self, assessment_id: str, app_name: str, reason: str,
        cluster_outcome: dict | None, delivery: dict,
    ) -> dict:
        """Terminal action once the router has picked the GitOps-registered
        infra-repo-commit mechanism for the cluster/app-config category
        (docs/unified-apply-flow.md section (B)): merging into a
        self-healing, ``prune: true`` GitOps repo is a bigger blast-radius
        grant than a direct apply, so the PR is opened autonomously but a
        human must still merge it -- AgentIT never auto-merges.
        ``route_and_deliver()`` already opened the PR (via
        ``deliver_with_verification``) and, on success, already created the
        ``gitops-pr-pending`` gate itself -- this only reacts to that
        result, it never re-opens the PR or re-creates the gate.
        """
        if isinstance(cluster_outcome, dict) and cluster_outcome.get("gate_id"):
            pr_url = cluster_outcome.get("pr_url", "")
            gate_id = cluster_outcome["gate_id"]
            await self._log_event(
                "auto-mode", "gitops-pr-opened", app_name, "info",
                f"Opened PR {pr_url} against the GitOps infra repo -- awaiting human merge (gate {gate_id})",
            )
            return {
                "action": "gated",
                "reason": f"GitOps-registered -- opened PR, awaiting human merge: {reason}",
                "details": {"delivery": delivery, "gate_id": gate_id},
            }

        error = cluster_outcome.get("error", "unknown error") if isinstance(cluster_outcome, dict) else "unknown error"
        await self._log_event(
            "auto-mode", "gitops-commit-failed", app_name, "warning",
            f"Commit to infra repo failed: {error}",
        )
        gate_id = await self._store.create_gate(
            assessment_id, "auto-mode-review",
            f"Auto-mode's GitOps commit failed: {error}",
        )
        return {"action": "gated", "reason": "gitops commit failed",
                "details": {"delivery": delivery, "gate_id": gate_id}}

    async def _finish_without_cluster_config_delivery(
        self, assessment_id: str, app_name: str, reason: str,
        cluster_outcome: dict | None, delivery: dict,
    ) -> dict:
        """Terminal action for every batch the router didn't route to the
        GitOps-PR mechanism above. Only two cases reach here now that
        Direct Apply has been removed as a concept entirely (cluster-config
        can only ever resolve to ``MECHANISM_INFRA_REPO_COMMIT`` -- handled
        by ``_finish_gitops_pr()`` above -- or ``MECHANISM_NONE``, handled
        below; `apply_manifests_to_cluster()`/`kube.apply_yaml()` are never
        called for this category at all anymore):

        - No cluster/app-config files in this batch at all (e.g. it was
          entirely source-patch/manifest-at-rest/CI-CD files, which the
          router already fully delivered/gated/blocked on its own).
        - A pre-delivery routing error (no known infra repo at all for this
          assessment) -- gated for human review with that reason, never a
          fallback to mutating the cluster directly.
        """
        if cluster_outcome is None:
            await self._log_event(
                "auto-mode", "auto-applied", app_name, "info",
                "Delivered via the unified router -- no cluster/app-config files in this batch",
            )
            await self._complete_remediations(assessment_id)
            return {"action": "applied", "reason": reason, "details": {"delivery": delivery}}

        error = cluster_outcome.get("error", "unknown error") if isinstance(cluster_outcome, dict) else "unknown error"
        await self._log_event(
            "auto-mode", "delivery-routing-error", app_name, "warning",
            f"Could not route cluster/app-config files: {error}",
        )
        gate_id = await self._store.create_gate(
            assessment_id, "auto-mode-review",
            f"Auto-mode could not route cluster/app-config files: {error}",
        )
        return {"action": "gated", "reason": "delivery routing error",
                "details": {"delivery": delivery, "gate_id": gate_id}}

    async def _complete_remediations(self, assessment_id: str) -> None:
        from agentit.portal.delivery import complete_remediations

        await complete_remediations(self._store, assessment_id)

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
