"""Auto-mode decision engine — determines whether to auto-apply or gate for human review."""

from __future__ import annotations

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.8

# ── Per-(namespace, resource-kind) auto-mode allowlist ─────────────────────
#
# Extends the single global on/off `auto_mode` setting (above) with an
# optional, additive scoping layer stored under its own key in the exact
# same `settings` table `get_setting`/`set_setting` already read and write
# (see `portal/store.py` -- no schema change needed, this is just another
# JSON-encoded value under a new key). Empty/missing/unparseable
# is treated as "no allowlist configured" -- `should_auto_apply()`'s global
# gate keeps deciding the whole batch exactly as it did before this existed,
# so an operator who has never touched this setting sees zero behavior
# change. Once an operator adds at least one pattern, `execute()`'s direct-
# apply path (the one place AutoMode mutates a cluster without a human
# already in the loop) partitions each apply batch per file/manifest-kind
# instead of treating it as all-or-nothing.
ALLOWLIST_SETTING_KEY = "auto_mode_allowlist"

# Kinds that grant or manage permissions/credentials -- these are never
# allowlistable for auto-mode, even by an explicit pattern naming them
# (e.g. `*/Secret` or `prod/ClusterRoleBinding` in the allowlist setting is
# silently ignored, not honored). Mirrors this codebase's existing
# permanent, non-overridable deny-rule for `Secret` in
# `portal/delivery.py`'s unified router (`CATEGORY_SECRET_BLOCKED`) and its
# own "never put secrets in values.yaml"-style conservative-by-default
# posture -- auto-mode is explicitly for low-risk, reversible changes
# (ConfigMaps, NetworkPolicies, ...), never for identity/RBAC/credentials.
RBAC_SHAPED_KINDS = frozenset({"Secret", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})


def parse_allowlist(raw: str | None) -> list[str]:
    """Parse the `auto_mode_allowlist` setting's raw string value into a
    list of `"<namespace-or-*>/<kind-or-*>"` patterns. Returns `[]` (i.e.
    "no allowlist configured") for `None`, invalid JSON, a non-list value,
    or any list entries that aren't `"ns/kind"`-shaped strings -- fails
    closed to "not configured" rather than raising, matching this module's
    existing `is_enabled()`/`should_auto_apply()` fail-closed convention.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [p for p in parsed if isinstance(p, str) and "/" in p]


def _serialize_allowlist(patterns: list[str]) -> str:
    return json.dumps(patterns)


async def add_allowlist_pattern(store: object, pattern: str) -> list[str]:
    """Add one `"namespace/kind"` pattern to the allowlist setting (no-op if
    already present). Returns the resulting full pattern list. Used by the
    Settings page's "Add" form (`routes/settings.py`)."""
    current = parse_allowlist(await store.get_setting(ALLOWLIST_SETTING_KEY))
    if pattern not in current:
        current.append(pattern)
        await store.set_setting(ALLOWLIST_SETTING_KEY, _serialize_allowlist(current))
    return current


async def remove_allowlist_pattern(store: object, pattern: str) -> list[str]:
    """Remove one pattern from the allowlist setting. Returns the resulting
    full pattern list. Used by the Settings page's per-row "Remove" form."""
    current = [p for p in parse_allowlist(await store.get_setting(ALLOWLIST_SETTING_KEY)) if p != pattern]
    await store.set_setting(ALLOWLIST_SETTING_KEY, _serialize_allowlist(current))
    return current


def _pattern_allows(patterns: list[str], namespace: str, kind: str) -> bool:
    for pattern in patterns:
        ns_pattern, _, kind_pattern = pattern.partition("/")
        if ns_pattern in ("*", namespace) and kind_pattern in ("*", kind):
            return True
    return False


def split_files_by_allowlist(
    files: list[dict], namespace: str, allowlist_patterns: list[str],
) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """Partition manifest files into (allowed, denied, denial_reasons)
    against a configured per-(namespace, kind) auto-mode allowlist.

    ``allowlist_patterns`` empty means "no allowlist configured" -- returns
    every file as allowed with no denials, a pure no-op that preserves
    today's whole-batch behavior exactly (the global `auto_mode` toggle
    keeps deciding everything until an operator opts in to scoping).

    Non-YAML files (e.g. a narrative report/summary) and YAML files with no
    parseable K8s document are always allowed through -- they're never
    actually applied to the cluster (`apply_manifests_to_cluster` routes
    them to `repo_files`/`skipped` on its own), so there's no cluster
    permission for this allowlist to scope.

    Splits per *file*, not per whole batch: a batch with one allowed
    ConfigMap and one disallowed ClusterRoleBinding returns the ConfigMap
    in ``allowed`` and the ClusterRoleBinding in ``denied`` rather than
    denying (or allowing) the whole batch. A single multi-document YAML
    file is still one atomic unit (matching `apply_manifests_to_cluster`'s
    own per-file, not per-document, granularity) -- if any document inside
    it is denied, the whole file is denied.
    """
    if not allowlist_patterns:
        return list(files), [], {}

    from agentit.portal.cluster_apply import _parse_manifest

    allowed: list[dict] = []
    denied: list[dict] = []
    reasons: dict[str, list[str]] = {}

    for f in files:
        path = f.get("path", "")
        if not path.endswith((".yaml", ".yml")):
            allowed.append(f)
            continue

        docs = _parse_manifest(f.get("content", ""))
        kinds = [d.get("kind", "") for d in docs if d.get("kind")]
        if not kinds:
            allowed.append(f)
            continue

        file_reasons: list[str] = []
        for doc in docs:
            kind = doc.get("kind", "")
            if not kind:
                continue
            doc_namespace = (doc.get("metadata") or {}).get("namespace") or namespace
            if kind in RBAC_SHAPED_KINDS:
                file_reasons.append(f"{doc_namespace}/{kind} is RBAC-shaped -- never auto-mode-allowlistable")
            elif not _pattern_allows(allowlist_patterns, doc_namespace, kind):
                file_reasons.append(f"{doc_namespace}/{kind} not in auto-mode allowlist")

        if file_reasons:
            denied.append(f)
            reasons[path] = file_reasons
        else:
            allowed.append(f)

    return allowed, denied, reasons


def _scope_gate_summary(denied_files: list[dict], denial_reasons: dict[str, list[str]]) -> str:
    parts = [f"{p}: {'; '.join(rs)}" for p, rs in denial_reasons.items()]
    return (
        f"Auto-mode allowlist excludes {len(denied_files)} manifest(s) from auto-apply -- "
        "outside the configured (namespace, resource-kind) scope. Review and apply manually, "
        "or add the relevant pattern(s) to the allowlist in Settings. "
        + " | ".join(parts)
    )[:1000]


class AutoMode:
    """Decides whether agent-generated manifests should be auto-applied or queued.

    Decision matrix:
      auto_mode OFF → always gate (current behavior)
      auto_mode ON + orchestrator auto_approve + LLM says safe → auto-apply
      auto_mode ON + orchestrator auto_approve + LLM says destructive → gate + alert
      auto_mode ON + orchestrator says no auto_approve → gate
      auto_mode ON + LLM unavailable → gate (fail-closed)

    A second, independent, additive scoping layer sits on top of this
    matrix's "auto-apply" outcome (see ``split_files_by_allowlist()`` above):
    once the matrix above says "auto-apply", ``execute()``'s direct-apply
    path further splits the batch per (namespace, kind) against the
    ``auto_mode_allowlist`` setting -- files outside that scope are gated
    for human review individually rather than either silently applied or
    failing the whole batch. With no allowlist configured (the default),
    this layer is a no-op and the matrix above is the whole story, exactly
    as before it existed.

    Once both of the above have approved a batch (``should_auto_apply()``'s
    safety classification, then the allowlist scoping), ``execute()`` hands
    that batch to ``portal/delivery.py::route_and_deliver()`` -- the same
    decision surface the manual "Deliver" button and gate-approve use (see
    docs/unified-apply-flow.md) -- for classification (secret-block,
    unresolved-placeholder guard, CI/CD-shared-namespace escalation),
    GitOps-registration lookup, and mechanism selection. This class no
    longer re-decides any of that on its own; it only supplies its own two
    extra, AutoMode-specific safety layers on top (the LLM classification
    and the allowlist scoping above), plus its own reaction to
    ``force_dry_run_first=True`` (a safety knob no other router caller
    exercises) once the router returns.

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

    async def get_allowlist(self) -> list[str]:
        """The configured per-(namespace, kind) auto-mode allowlist patterns,
        or ``[]`` if none are configured (fail-closed to "not configured" --
        i.e. "no scoping" -- on a store read failure, matching
        ``is_enabled()``'s convention above; ``execute()`` only uses this to
        additionally *restrict* an already-permitted batch, so failing to
        "no scoping configured" here can never widen what gets auto-applied
        beyond what the rest of the decision matrix already allowed)."""
        try:
            raw = await self._store.get_setting(ALLOWLIST_SETTING_KEY)
        except Exception as exc:
            logger.warning("Failed to read auto_mode_allowlist setting, defaulting to none configured: %s", exc)
            return []
        return parse_allowlist(raw)

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
        report: object | None = None,
    ) -> dict:
        """Full auto-apply pipeline: dry-run → classify → apply or gate.

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

        Returns {"action": "applied"|"split"|"partial_failure"|"gated"|"failed", "reason": str, "details": dict}.
        ``"split"`` only occurs when the auto-mode allowlist denied part (not
        all) of the batch and the allowed remainder delivered cleanly. A real
        server-side-apply field-manager conflict can no longer occur for the
        cluster-config category at all (`apply_manifests_to_cluster()`/
        `kube.apply_yaml()` are never called for it anymore -- see
        `route_and_deliver()`); the historical `cluster-conflict-review` gate
        type this used to create for that case has been removed along with
        Direct Apply.
        """
        from agentit.portal.delivery import CATEGORY_CLUSTER_CONFIG, MECHANISM_INFRA_REPO_COMMIT, route_and_deliver

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

        # Per-(namespace, kind) allowlist scoping -- see
        # ``split_files_by_allowlist()``'s docstring above. A no-op (returns
        # every file as ``allowed_files``, ``denied_files`` empty) unless an
        # operator has configured the ``auto_mode_allowlist`` setting, so
        # this can never change behavior for anyone who hasn't opted in.
        # This is AutoMode's own additive safety layer, applied *before*
        # handing off to the shared router below -- not a replacement for
        # anything the router does.
        allowlist = await self.get_allowlist()
        allowed_files, denied_files, denial_reasons = split_files_by_allowlist(files, namespace, allowlist)

        scope_gate_id: str | None = None
        if denied_files:
            scope_gate_id = await self._store.create_gate(
                assessment_id, "auto-mode-scope-review",
                _scope_gate_summary(denied_files, denial_reasons),
            )
            await self._log_event(
                "auto-mode", "scope-gated", app_name, "warning",
                f"{len(denied_files)} manifest(s) outside auto-mode allowlist scope -- "
                f"gated for human review (gate {scope_gate_id})",
            )
            if not allowed_files:
                return {
                    "action": "gated",
                    "reason": "all manifests are outside the configured auto-mode allowlist scope",
                    "details": {"gate_id": scope_gate_id, "denied": denial_reasons},
                }

        # Everything from here on delegates to the exact same decision
        # surface every other delivery-triggering caller uses (manual
        # Deliver, gate-approve, docs/unified-apply-flow.md): classify each
        # of ``allowed_files`` (secret-block, unresolved-placeholder guard,
        # CI/CD-shared-namespace escalation all now apply uniformly, where
        # before this refactor none of them did for AutoMode), look up
        # GitOps registration, and pick the mechanism per classified group.
        # ``force_dry_run_first=True`` is AutoMode's own, unchanged safety
        # knob, threaded straight through to the router's direct-apply
        # branch -- no other caller sets this ``True`` today, so reacting to
        # its outcome (``dry_run_failed``/conflicts) below is AutoMode's own
        # safety layer on top of the router, not a duplication of it.
        delivery = await route_and_deliver(
            allowed_files, app_name=app_name, namespace=namespace, report=report,
            store=self._store, assessment_id=assessment_id, actor="auto-mode",
            dry_run=False, force_dry_run_first=True,
        )

        mechanism_used = delivery["mechanisms"].get(CATEGORY_CLUSTER_CONFIG)
        cluster_outcome = delivery["outcomes"].get(CATEGORY_CLUSTER_CONFIG)
        split_extra = {"scope_gate_id": scope_gate_id, "denied_files": list(denial_reasons)} if denied_files else {}

        if mechanism_used == MECHANISM_INFRA_REPO_COMMIT:
            return await self._finish_gitops_pr(assessment_id, app_name, reason, cluster_outcome, delivery, split_extra)

        return await self._finish_direct_apply(
            assessment_id, app_name, namespace, reason, cluster_outcome, delivery,
            bool(denied_files), split_extra,
        )

    async def _finish_gitops_pr(
        self, assessment_id: str, app_name: str, reason: str,
        cluster_outcome: dict | None, delivery: dict, split_extra: dict,
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
                "details": {"delivery": delivery, "gate_id": gate_id, **split_extra},
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
                "details": {"delivery": delivery, "gate_id": gate_id, **split_extra}}

    async def _finish_direct_apply(
        self, assessment_id: str, app_name: str, namespace: str, reason: str,
        cluster_outcome: dict | None, delivery: dict, split: bool, split_extra: dict,
    ) -> dict:
        """Terminal action for every category the router didn't route to
        the GitOps-PR mechanism above -- direct cluster apply for
        cluster/app-config (the common case), plus whatever the router
        already did on its own for any CI/CD-shared-namespace/source-patch/
        manifest-at-rest/secret-blocked files in the same batch (fully
        handled by ``route_and_deliver()`` itself; nothing further to do
        for those here).

        ``cluster_outcome`` is ``apply_with_verification()``'s own return
        value, passed through by the router untouched -- so AutoMode's
        pre-existing reaction to ``dry_run_failed``/conflicts (its own
        ``force_dry_run_first=True`` safety net, which no other router
        caller exercises) still works exactly as before, just fed from here
        instead of a direct call.
        """
        if cluster_outcome is None:
            # No cluster/app-config files in this allowed batch at all --
            # e.g. it was entirely source-patch/manifest-at-rest/CI-CD
            # files, which the router already fully delivered/gated/blocked
            # above. Nothing to gate here on a dry-run-failure/conflict
            # basis since no direct apply was ever attempted.
            await self._log_event(
                "auto-mode", "auto-applied", app_name, "info",
                "Delivered via the unified router -- no cluster/app-config files in this batch",
            )
            await self._complete_remediations(assessment_id)
            return {"action": "split" if split else "applied", "reason": reason,
                    "details": {"delivery": delivery, **split_extra}}

        if "error" in cluster_outcome and "errors" not in cluster_outcome:
            # A pre-apply routing error (e.g. the router's own
            # registered-but-no-known-infra_repo_url edge case) that never
            # reached apply_with_verification() at all -- gate for human
            # review rather than assume apply_with_verification()'s
            # errors/conflicts/dry_run_failed keys exist.
            error = cluster_outcome["error"]
            await self._log_event(
                "auto-mode", "delivery-routing-error", app_name, "warning",
                f"Could not route cluster/app-config files: {error}",
            )
            gate_id = await self._store.create_gate(
                assessment_id, "auto-mode-review",
                f"Auto-mode could not route cluster/app-config files: {error}",
            )
            return {"action": "gated", "reason": "delivery routing error",
                    "details": {"delivery": delivery, "gate_id": gate_id, **split_extra}}

        # `cluster_outcome.get("dry_run_failed")`/a real server-side-apply
        # conflict (`cluster_outcome.get("conflicts")`) can no longer
        # genuinely occur here -- both require `apply_with_verification()`/
        # `apply_manifests_to_cluster()` to have actually been called for
        # the cluster-config category, which never happens anymore (Direct
        # Apply removed as a concept entirely; see `route_and_deliver()`).
        # The branches below are kept only as defensive handling in case
        # `cluster_outcome` is ever produced some other way in the future --
        # they no longer create a dedicated `cluster-conflict-review` gate
        # for a conflict (that gate type has been removed along with Direct
        # Apply); a conflict is just one more reason folded into the same
        # generic dry-run-failed/partial-failure gating as any other error.
        if cluster_outcome.get("dry_run_failed"):
            failure_notes = list(cluster_outcome["errors"])
            if cluster_outcome.get("conflicts"):
                failure_notes.extend(
                    c.get("error", "field-manager conflict") for c in cluster_outcome["conflicts"]
                )
            await self._log_event(
                "auto-mode", "dry-run-failed", app_name, "warning",
                f"Dry-run failed with {len(failure_notes)} error(s)",
            )
            gate_id = await self._store.create_gate(
                assessment_id, "dry-run-failed",
                f"Dry-run failed: {'; '.join(failure_notes[:3])}",
            )
            return {"action": "gated", "reason": "dry-run failed",
                    "details": {**cluster_outcome, "delivery": delivery, "gate_id": gate_id, **split_extra}}

        if cluster_outcome["errors"] or cluster_outcome.get("conflicts"):
            details = {**cluster_outcome, "delivery": delivery, **split_extra}
            await self._log_event(
                "auto-mode", "partial-failure", app_name, "warning",
                f"Applied {len(cluster_outcome['applied'])} but {len(cluster_outcome['errors'])} error(s), "
                f"{len(cluster_outcome.get('conflicts', []))} conflict(s) in {namespace}",
            )
            return {
                "action": "partial_failure",
                "reason": f"partial failure: {len(cluster_outcome['errors'])} error(s), {len(cluster_outcome.get('conflicts', []))} conflict(s)",
                "details": details,
            }

        await self._log_event(
            "auto-mode", "auto-applied", app_name, "info",
            f"Applied {len(cluster_outcome['applied'])} manifests to {namespace}",
        )
        await self._complete_remediations(assessment_id)
        return {
            "action": "split" if split else "applied",
            "reason": reason,
            "details": {**cluster_outcome, "delivery": delivery, **split_extra},
        }

    async def _complete_remediations(self, assessment_id: str) -> None:
        remediations = await self._store.list_remediations(assessment_id)
        for rem in remediations:
            if rem["status"] != "completed":
                await self._store.complete_remediation(rem["id"])

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
