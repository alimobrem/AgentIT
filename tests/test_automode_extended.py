"""Extended auto-mode tests — execute pipeline, LLM integration, edge cases."""

from __future__ import annotations

import logging

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from agentit.llm import LLMClient
from conftest import make_async_store, make_report


class TestExecuteAutoApply:
    async def test_auto_apply_with_safe_llm(self):
        """Direct Apply has been removed as a concept entirely -- once
        GitOps-registered (a known infra_repo_url), AutoMode's "safe"
        terminal action is a GitOps commit+PR, never a direct apply."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.95,
            "reason": "Adds ConfigMap",
        }

        files = [
            {"category": "cost", "path": "labels.yaml",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
             "description": "labels"},
        ]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        assert "safe" in result["reason"]
        mock_commit.assert_called_once()
        mock_apply.assert_not_called()

    async def test_no_infra_repo_gates_with_a_routing_error_no_direct_apply_fallback(self):
        """Direct Apply has been removed as a concept entirely -- an
        AutoMode-approved batch for an app with no known infra repo at all
        (only possible for an assessment saved before GitOps registration
        became mandatory) is gated for human review with a clear routing
        error, never silently applied directly to the cluster."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False,
            "confidence": 0.95,
            "reason": "Safe",
        }

        files = [{"category": "sec", "path": "np.yaml",
                  "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
                  "description": "np"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        assert "routing error" in result["reason"]
        mock_apply.assert_not_called()

    async def test_remediations_stay_pending_until_gitops_pr_is_merged(self):
        """Documents a real, discovered behavior change from Direct Apply's
        removal (flagged in the task report, not silently papered over):
        `_finish_direct_apply()`'s success branch marked remediations
        "completed" the instant a direct apply succeeded -- appropriate,
        since a direct apply mutates the cluster immediately.
        `_finish_gitops_pr()` never calls `_complete_remediations()` at all
        -- opening a PR is not delivery; the cluster is not mutated until a
        human merges it. Since GitOps commit+PR is now cluster_config's ONLY
        reachable outcome (Direct Apply removed entirely), remediations tied
        to a cluster_config fix now stay "generated", not "completed", after
        AutoMode's terminal action -- arguably the more honest state (it
        genuinely isn't done until merged), but a real, visible behavior
        change worth a human decision on whether completion should instead
        be wired to the eventual PR-merge event."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        await raw.save_remediation(aid, "security", "Add NetworkPolicy")

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }

        # Real, parseable NetworkPolicy content (not the old placeholder
        # "x") -- execute() now routes through delivery.py's classify_file(),
        # which sorts unparseable YAML into manifest-at-rest, not
        # cluster_config, so a real manifest is needed to reach the commit.
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, [{
                "path": "np.yaml", "category": "sec", "description": "np",
                "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
            }], "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        rems = await raw.list_remediations(aid)
        assert rems[0]["status"] != "completed"


class TestExecuteNoLongerDryRunsAgainstTheClusterForClusterConfig:
    """AutoMode's real, deliberately-preserved distinction from the manual
    Deliver route USED TO be: it always dry-ran directly against the
    cluster first, regardless of what the eventual real-apply outcome would
    be, and never skipped straight to a real apply. That distinction was
    specific to the direct-apply mechanism (`apply_with_verification()`'s
    own forced-dry-run-then-real-apply sequence), removed along with Direct
    Apply as a concept entirely -- a GitOps commit+PR is a single
    `commit_to_infra_repo()` call, never preceded by a live-cluster dry run.
    (Step 5 -- `cluster_apply.py`'s dead code removal -- separately verifies
    whether Dry Run should still validate a GitOps-bound manifest against
    the live cluster for schema/CRD errors before it's ever committed; that
    is a distinct concern from this AutoMode-specific double-apply
    sequence, which genuinely no longer applies here.)"""

    async def test_gitops_commit_is_a_single_call_never_preceded_by_a_cluster_dry_run(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_remediations_stay_pending_until_gitops_pr_is_merged above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        mock_commit.assert_called_once()
        mock_apply.assert_not_called()


class TestExecuteAuditLogGapClosed:
    """Real gap fix: before this refactor, AutoMode.execute() never called
    audit_log() at all (only the manual route did). These confirm the
    shared apply_with_verification() closes that for every real exit path."""

    async def test_audit_log_fires_on_successful_auto_apply(self, caplog):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        # Real, parseable ConfigMap content -- see the comment on
        # test_remediations_stay_pending_until_gitops_pr_is_merged above for why.
        files = [{
            "category": "cost", "path": "labels.yaml", "description": "labels",
            "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        mock_apply.assert_not_called()
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].actor == "auto-mode"
        # "deliver", not "deliver-apply" -- AutoMode's GitOps commit+PR now
        # goes through the exact same deliver_with_verification() call site
        # (inside route_and_deliver()) every other commit/PR-based caller
        # uses, so it shares that caller's action label instead of a
        # separate one. Direct Apply's own "deliver-apply" label (still used
        # by cluster-admin-review's own direct apply into a shared operator
        # namespace, an unrelated code path) is no longer reachable here.
        assert audit_records[0].action == "deliver"
        assert audit_records[0].resource == f"assessment:{aid}"
        assert audit_records[0].outcome == "success"

    async def test_no_audit_log_when_gated_with_no_infra_repo_routing_error(self, caplog):
        """Direct Apply has been removed as a concept entirely -- an app
        with no known infra repo at all never reaches
        apply_with_verification()/deliver_with_verification() (nothing was
        genuinely attempted), so there's nothing to audit for the
        cluster-config category -- unlike the pre-removal "dry-run failed"
        case, which DID audit a real, attempted apply."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_remediations_stay_pending_until_gitops_pr_is_merged above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        assert "routing error" in result["reason"]
        mock_apply.assert_not_called()
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 0

    async def test_no_audit_log_when_gated_before_apply_attempted(self, caplog):
        """auto_approve=False gates before apply_with_verification is ever
        called -- no cluster-apply audit entry should appear (there was
        nothing to audit yet)."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        engine = AutoMode(store=s, llm_client=None)
        with caplog.at_level(logging.INFO, logger="agentit.audit"):
            result = await engine.execute(aid, [], "default", "high", False, "app")

        assert result["action"] == "gated"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 0


class TestExecuteGitopsAwareTerminalAction:
    """docs/unified-apply-flow.md section (B): `should_auto_apply()`'s
    safety classification is unchanged -- only the *terminal action* once
    `can_apply` is True differs based on GitOps registration."""

    async def test_gitops_registered_opens_pr_and_gates_for_human_merge(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        files = [{"category": "skills", "path": "app-cost-labels.yaml",
                  "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
                  "description": "labels"}]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/infra-gitops/pull/7",
                "commit_url": "https://github.com/org/infra-gitops/commit/deadbeef",
                "files_committed": 1,
            }
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(
                aid, files, "default", "low", True, "test-app", report=report,
            )

        assert result["action"] == "gated"
        assert "awaiting human merge" in result["reason"]
        mock_apply.assert_not_called()
        mock_commit.assert_called_once()

        gates = await raw.list_gates(status="pending")
        assert len(gates) == 1
        assert gates[0]["gate_type"] == "gitops-pr-pending"
        assert "pull/7" in gates[0]["summary"]
        assert "never auto-merge" in gates[0]["summary"]

    async def test_report_omitted_gates_with_no_direct_apply_fallback(self):
        """Direct Apply has been removed as a concept entirely -- a caller
        that omits `report` (no way to know an infra_repo_url at all) is
        gated for human review with a routing error, never silently applied
        directly to the cluster."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        files = [{"category": "cost", "path": "labels.yaml",
                  "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
                  "description": "labels"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "routing error" in result["reason"]
        mock_apply.assert_not_called()


class TestExecuteConflictHandlingIsNowUnreachableForClusterConfig:
    """`AutoMode.execute()`'s reaction to `kube.apply_yaml()`'s structured
    server-side-apply conflict result used to be surfaced through
    `apply_with_verification()`'s `conflicts` list, for the cluster-config
    category specifically: never silently forced, never lumped in with a
    generic partial failure -- always routed to a dedicated
    `cluster-conflict-review` gate.

    That whole path is provably unreachable now: `apply_manifests_to_
    cluster()`/`kube.apply_yaml()` are never called for cluster-config at
    all (mechanism is always `infra-repo-commit` or `none`, never
    `direct-apply`), so no server-side-apply conflict can ever occur for
    this category, so `_gate_for_conflicts()`/`cluster-conflict-review` can
    never be created via this path. (This dead code -- and this test class
    -- is exactly what Step 3 of the Direct Apply removal removes; kept and
    updated here only to prove Step 2's mechanism change didn't leave a
    silent, subtly-broken conflict-handling path behind.)"""

    async def test_no_infra_repo_gates_with_a_routing_error_never_a_conflict_review(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_remediations_stay_pending_until_gitops_pr_is_merged above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        assert "routing error" in result["reason"]
        mock_apply.assert_not_called()
        gates = await raw.list_gates(status="pending")
        assert not any(g["gate_type"] == "cluster-conflict-review" for g in gates)
        assert any(g["gate_type"] == "auto-mode-review" for g in gates)

    async def test_gitops_registered_never_creates_a_conflict_review_gate(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        assert result["action"] == "gated"
        mock_apply.assert_not_called()
        gates = await raw.list_gates(status="pending")
        assert not any(g["gate_type"] == "cluster-conflict-review" for g in gates)
        assert any(g["gate_type"] == "gitops-pr-pending" for g in gates)


class TestExecuteUnifiedRouterGuards:
    """The bug this refactor fixes: `execute()` used to hand-roll its own
    routing (GitOps-registration check, then a raw `apply_with_verification`
    call on the whole allowed batch) and never called
    `delivery.py::classify_file()` at all -- so none of the guards every
    other delivery-triggering caller (manual Deliver, gate-approve) gets for
    free via `route_and_deliver()` ever applied to AutoMode's own auto-apply
    path. `execute()` now calls `route_and_deliver()` for the actual
    routing/classification/mechanism decision instead of reimplementing it.

    Confirmed against the pre-fix code (temporarily reverting `automode.py`
    to its prior direct `apply_with_verification()` call and re-running
    these two tests) that both gaps are real: the Secret test's
    `mock_apply.assert_not_called()`/`"secret.yaml" not in paths` assertions
    failed (the Secret WAS handed to `apply_manifests_to_cluster`), and the
    CI/CD test's gate assertion failed with zero `cluster-admin-review`
    gates created (the Pipeline was silently `skip_operator_ns`'d inside
    `cluster_apply.py` instead). Both pass against the fixed code below.
    """

    async def test_secret_manifest_never_reaches_cluster_apply(self):
        """(a) A Secret-kind manifest riding along in an otherwise-safe
        AutoMode batch must never reach `apply_manifests_to_cluster` --
        the same `CATEGORY_SECRET_BLOCKED` permanent deny-rule a manual
        Deliver click already gets for free via `classify_file()`. The
        ConfigMap in the same batch must still be committed normally (via
        GitOps -- Direct Apply has been removed as a concept entirely)."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [
            {"category": "skills", "path": "cm.yaml", "description": "configmap",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n"},
            {"category": "skills", "path": "secret.yaml", "description": "should never be delivered",
             "content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: c2VjcmV0\n"},
        ]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        # The Secret must never have been handed to apply_manifests_to_cluster
        # (never called at all for cluster-config anymore) or committed.
        mock_apply.assert_not_called()
        mock_commit.assert_called_once()
        committed_paths = {f["path"] for f in mock_commit.call_args[0][2]}
        assert committed_paths == {"cm.yaml"}
        assert result["action"] == "gated"
        assert result["details"]["delivery"]["blocked"] == ["secret.yaml"]

    async def test_cicd_shared_namespace_escalates_to_admin_review_gate(self):
        """(b) A CI/CD manifest destined for a shared operator namespace
        (e.g. `openshift-pipelines`) must escalate to a `cluster-admin-review`
        gate -- never `cluster_apply.py`'s older, silent `skip_operator_ns`
        behavior every other AutoMode-generated manifest kind gets."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [{
            "category": "skills", "path": "pipeline.yaml", "description": "tekton pipeline",
            "content": (
                "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
                "metadata:\n  name: build\n  namespace: openshift-pipelines\n"
            ),
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            # Only relevant pre-fix: the real (unmocked) function's own
            # skip_operator_ns classification for this exact manifest.
            mock_apply.return_value = {
                "applied": [], "errors": [],
                "skipped": ["pipeline.yaml (targets operator namespace openshift-pipelines)"],
            }
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        # Never routed through cluster_apply.py at all -- no chance of the
        # old silent skip_operator_ns behavior.
        mock_apply.assert_not_called()
        gates = await raw.list_gates(status="pending")
        admin_gates = [g for g in gates if g["gate_type"] == "cluster-admin-review"]
        assert len(admin_gates) == 1
        assert "openshift-pipelines" in admin_gates[0]["summary"]
        assert "never a silent skip" in admin_gates[0]["summary"]
        delivery = result["details"]["delivery"]
        assert delivery["mechanisms"]["cicd_shared_namespace"] == "cluster-admin-review-gate"
        assert delivery["outcomes"]["cicd_shared_namespace"]["gate_id"] == admin_gates[0]["id"]

    async def test_mixed_batch_commits_cluster_config_and_gates_cicd_separately(self):
        """A single AutoMode batch mixing an ordinary ConfigMap with a
        CI/CD-shared-namespace manifest must split correctly: the ConfigMap
        is committed via GitOps (Direct Apply has been removed as a concept
        entirely), the CI/CD manifest gets its own admin-review gate -- both
        guards apply in the same call, not just in isolation. The CI/CD
        lane's `cluster-admin-review` escalation is completely independent
        of the cluster-config category's own mechanism -- it still applies
        directly into the shared operator namespace once a human approves
        it (see routes/gates.py), unrelated to Direct Apply's removal."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [
            {"category": "skills", "path": "cm.yaml", "description": "configmap",
             "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n"},
            {"category": "skills", "path": "pipeline.yaml", "description": "tekton pipeline",
             "content": (
                 "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
                 "metadata:\n  name: build\n  namespace: openshift-pipelines\n"
             )},
        ]

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app", report=report)

        mock_apply.assert_not_called()
        mock_commit.assert_called_once()
        committed_paths = {f["path"] for f in mock_commit.call_args[0][2]}
        assert committed_paths == {"cm.yaml"}
        assert result["action"] == "gated"
        gates = await raw.list_gates(status="pending")
        assert any(g["gate_type"] == "cluster-admin-review" for g in gates)
        assert any(g["gate_type"] == "gitops-pr-pending" for g in gates)


class TestExecuteWithPublisher:
    async def test_publishes_events(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        publisher = MagicMock()
        engine = AutoMode(store=s, publisher=publisher, llm_client=None)
        await engine.execute(aid, [], "default", "high", False, "test-app")

        assert publisher.publish.called
        call_args = publisher.publish.call_args
        assert call_args[1]["agent_id"] == "auto-mode"


class TestLLMClassifyAction:
    def _make_client(self):
        with patch("agentit.llm._create_client"):
            return LLMClient(model="test")

    def test_classify_action_returns_dict(self):
        client = self._make_client()
        with patch.object(client, "_chat", return_value='{"is_destructive": false, "confidence": 0.9, "reason": "Adds ConfigMap"}'):
            result = client.classify_action("apply", ["kind: ConfigMap"], "test context")
        assert result is not None
        assert result["is_destructive"] is False
        assert result["confidence"] == 0.9

    def test_classify_action_returns_none_on_bad_json(self):
        client = self._make_client()
        with patch.object(client, "_chat", return_value="not json"):
            result = client.classify_action("apply", ["kind: Pod"], "ctx")
        assert result is None

    def test_classify_action_returns_none_on_llm_failure(self):
        client = self._make_client()
        with patch.object(client, "_chat", return_value=None):
            result = client.classify_action("apply", ["kind: Pod"], "ctx")
        assert result is None
