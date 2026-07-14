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
# (see `portal/store.py`/`store_pg.py` -- no schema change needed, this is
# just another JSON-encoded value under a new key). Empty/missing/unparseable
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


def _conflict_gate_summary(conflicts: list[dict]) -> str:
    parts = []
    for c in conflicts[:5]:
        detail_msgs = "; ".join(d.get("message", "") for d in (c.get("details") or []))
        parts.append(f"{c.get('path', '?')}: {detail_msgs or c.get('error', '')}")
    return (
        f"{len(conflicts)} manifest(s) hit a server-side-apply field-manager conflict -- "
        "another manager already owns the conflicting field(s), so AgentIT did not force "
        "through. Approving this gate re-applies with force=True, seizing ownership from "
        "the other manager. "
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
        `routes/webhooks.py`'s `webhook_auto_apply`/`webhook_finding`) is AutoMode's
        *only* new input for the unified apply flow: `should_auto_apply()`'s safety
        classification above is completely unchanged by it. It only decides this
        method's *terminal action* once `can_apply` is True -- direct-apply when the
        app isn't GitOps-registered (today's unchanged behavior), or an autonomous
        commit-and-PR-but-human-must-merge when it is (see docs/unified-apply-flow.md
        section (B)). Omitting `report` (every pre-existing caller/test) is always
        treated as "not GitOps-registered", preserving the exact prior direct-apply
        behavior.

        Returns {"action": "applied"|"split"|"partial_failure"|"gated"|"failed", "reason": str, "details": dict}.
        ``"split"`` only occurs when the auto-mode allowlist denied part (not
        all) of the batch and the allowed remainder applied cleanly; a
        real server-side-apply field-manager conflict always returns
        ``"gated"`` (dry-run stage) or ``"partial_failure"`` (real-apply
        stage) with a ``cluster-conflict-review`` gate, never ``"applied"``.
        """
        from agentit.portal.cluster_apply import apply_with_verification
        from agentit.portal.delivery import (
            MECHANISM_INFRA_REPO_COMMIT,
            confirmation_text,
            deliver_with_verification,
            is_gitops_registered,
        )

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

        registered, infra_repo_url = await is_gitops_registered(app_name, report)

        if registered and infra_repo_url is not None:
            # GitOps-aware terminal action: AutoMode already trusts its own
            # safety classification enough to skip human review for a direct
            # apply -- but merging into a self-healing, prune:true GitOps
            # repo is a bigger blast-radius grant than a single kubectl
            # apply, so the PR is opened autonomously and a human must still
            # merge it. AgentIT never auto-merges, matching this project's
            # own "Argo CD is the sole deployer" stance for itself.
            commit_result = await deliver_with_verification(
                mechanism=MECHANISM_INFRA_REPO_COMMIT, files=files, report=report,
                app_name=app_name, store=self._store, assessment_id=assessment_id,
                actor="auto-mode", dry_run=False,
            )
            if "error" in commit_result:
                await self._log_event(
                    "auto-mode", "gitops-commit-failed", app_name, "warning",
                    f"Commit to infra repo failed: {commit_result['error']}",
                )
                gate_id = await self._store.create_gate(
                    assessment_id, "auto-mode-review",
                    f"Auto-mode's GitOps commit failed: {commit_result['error']}",
                )
                return {"action": "gated", "reason": "gitops commit failed", "details": commit_result}

            pr_url = commit_result.get("pr_url", "")
            mechanism_text = confirmation_text(MECHANISM_INFRA_REPO_COMMIT, infra_repo_url=infra_repo_url)
            gate_id = await self._store.create_gate(
                assessment_id, "gitops-pr-pending",
                f"{mechanism_text} PR opened: {pr_url}. Reason: {reason}. "
                "Approving this gate merges the PR -- AgentIT never auto-merges.",
            )
            await self._log_event(
                "auto-mode", "gitops-pr-opened", app_name, "info",
                f"Opened PR {pr_url} against the GitOps infra repo -- awaiting human merge (gate {gate_id})",
            )
            return {
                "action": "gated",
                "reason": f"GitOps-registered -- opened PR, awaiting human merge: {reason}",
                "details": {**commit_result, "gate_id": gate_id},
            }

        # Per-(namespace, kind) allowlist scoping -- see
        # ``split_files_by_allowlist()``'s docstring above. A no-op (returns
        # every file as ``allowed_files``, ``denied_files`` empty) unless an
        # operator has configured the ``auto_mode_allowlist`` setting, so
        # this can never change behavior for anyone who hasn't opted in.
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

        # Dry-run first, then apply for real -- ``force_dry_run_first=True``
        # is auto-mode's own safety gate (unlike the manual "Apply to
        # Cluster" route, which just does whatever the human explicitly
        # asked for via its own ``dry_run`` flag with no automatic
        # dry-run-first sequencing). Per-skill outcome recording is
        # deliberately gated on a *fully clean* real apply here (see
        # ``record_outcomes_on_partial_failure=False`` below): "gated" is a
        # deferral to a human, not a verdict on the skill, and a partial
        # failure is intentionally left unrecorded too (the human's eventual
        # decision at /gates is what records approved/rejected for a gated
        # candidate). ``audit_log()`` (inside ``apply_with_verification``)
        # now covers auto-mode's own privileged apply action too, matching
        # the manual route -- previously auto-mode had no audit trail here
        # at all, only the ``_log_event``/publisher calls below.
        #
        # ``force`` is never passed here (stays at ``apply_with_verification``'s
        # default of ``False``) -- AutoMode never seizes field-manager
        # ownership on its own; a genuine conflict below always routes to
        # the ``cluster-conflict-review`` gate instead, whose *human*
        # approval path in ``routes/gates.py`` is the only place that
        # passes ``force=True``.
        result = await apply_with_verification(
            allowed_files, namespace, dry_run=False,
            force_dry_run_first=True,
            store=self._store, app_name=app_name,
            skill_outcome_reason=reason,
            record_outcomes_on_partial_failure=False,
            actor="auto-mode", action="auto-apply",
            resource=f"assessment:{assessment_id}",
        )

        if result["dry_run_failed"]:
            if result.get("conflicts") and not result["errors"]:
                gate_id = await self._gate_for_conflicts(assessment_id, app_name, result["conflicts"])
                return {"action": "gated", "reason": "field-manager conflict(s) detected", "details": {**result, "gate_id": gate_id}}
            await self._log_event(
                "auto-mode", "dry-run-failed", app_name, "warning",
                f"Dry-run failed with {len(result['errors'])} error(s)",
            )
            gate_id = await self._store.create_gate(
                assessment_id, "dry-run-failed",
                f"Dry-run failed: {'; '.join(result['errors'][:3])}",
            )
            return {"action": "gated", "reason": "dry-run failed", "details": result}

        if result["errors"] or result.get("conflicts"):
            details = result
            if result.get("conflicts"):
                gate_id = await self._gate_for_conflicts(assessment_id, app_name, result["conflicts"])
                details = {**result, "gate_id": gate_id}
            await self._log_event(
                "auto-mode", "partial-failure", app_name, "warning",
                f"Applied {len(result['applied'])} but {len(result['errors'])} error(s), "
                f"{len(result.get('conflicts', []))} conflict(s) in {namespace}",
            )
            return {
                "action": "partial_failure",
                "reason": f"partial failure: {len(result['errors'])} error(s), {len(result.get('conflicts', []))} conflict(s)",
                "details": details,
            }

        await self._log_event(
            "auto-mode", "auto-applied", app_name, "info",
            f"Applied {len(result['applied'])} manifests to {namespace}",
        )

        remediations = await self._store.list_remediations(assessment_id)
        for rem in remediations:
            if rem["status"] != "completed":
                await self._store.complete_remediation(rem["id"])

        return {
            "action": "applied" if not denied_files else "split",
            "reason": reason,
            "details": {
                **result,
                **({"scope_gate_id": scope_gate_id, "denied_files": list(denial_reasons)} if denied_files else {}),
            },
        }

    async def _gate_for_conflicts(self, assessment_id: str, app_name: str, conflicts: list[dict]) -> str:
        """Create (or reuse a pending) ``cluster-conflict-review`` gate for a
        real server-side-apply field-manager conflict -- the one caller-level
        reaction ``kube.apply_yaml()``'s structured conflict result exists
        to enable: never silently fail, never blindly force, always route to
        a human who can decide whether to seize ownership (see
        ``routes/gates.py``'s resolution handler for this gate type, the
        only code path that ever passes ``force=True``)."""
        gate_id = await self._store.create_gate(
            assessment_id, "cluster-conflict-review", _conflict_gate_summary(conflicts),
        )
        await self._log_event(
            "auto-mode", "conflict-detected", app_name, "warning",
            f"{len(conflicts)} field-manager conflict(s) detected -- gated for human review (gate {gate_id})",
        )
        return gate_id

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
