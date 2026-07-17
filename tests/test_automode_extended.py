"""Extended auto-mode tests — execute pipeline, LLM integration, edge cases."""

from __future__ import annotations

import logging

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from agentit.llm import LLMClient
from conftest import make_async_store, make_report


class TestExecuteAutoApply:
    async def test_auto_apply_with_safe_llm(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
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

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["labels.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        assert "safe" in result["reason"]

    async def test_dry_run_failure_gates(self):
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
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["forbidden"]}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "dry-run" in result["reason"]

    async def test_marks_remediations_complete_on_apply(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)
        await raw.save_remediation(aid, "security", "Add NetworkPolicy")

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }

        # Real, parseable NetworkPolicy content (not the old placeholder
        # "x") -- execute() now routes through delivery.py's classify_file(),
        # which sorts unparseable YAML into manifest-at-rest, not
        # cluster_config, so a real manifest is needed to reach mock_apply.
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            await engine.execute(aid, [{
                "path": "np.yaml", "category": "sec", "description": "np",
                "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
            }], "default", "low", True, "test-app")

        rems = await raw.list_remediations(aid)
        assert rems[0]["status"] == "completed"


class TestExecuteDryRunFirstAlwaysEnforced:
    """AutoMode's real, deliberately-preserved distinction from the manual
    "Apply to Cluster" route: it always dry-runs first, regardless of what
    the eventual real-apply outcome would be, and never skips straight to a
    real apply."""

    async def test_dry_run_called_before_real_apply(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_marks_remediations_complete_on_apply above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        assert mock_apply.call_count == 2
        first_call, second_call = mock_apply.call_args_list
        assert first_call.args[2] is True, "first call must be a dry-run"
        assert second_call.args[2] is False, "second call must be the real apply"


class TestExecuteAuditLogGapClosed:
    """Real gap fix: before this refactor, AutoMode.execute() never called
    audit_log() at all (only the manual route did). These confirm the
    shared apply_with_verification() closes that for every real exit path."""

    async def test_audit_log_fires_on_successful_auto_apply(self, caplog):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        # Real, parseable ConfigMap content -- see the comment on
        # test_marks_remediations_complete_on_apply above for why.
        files = [{
            "category": "cost", "path": "labels.yaml", "description": "labels",
            "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["labels.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].actor == "auto-mode"
        # "deliver-apply", not the old "auto-apply" -- AutoMode's direct-apply
        # now goes through the exact same apply_with_verification() call site
        # (inside route_and_deliver()) every other caller uses, so it shares
        # that caller's action label instead of a separate one.
        assert audit_records[0].action == "deliver-apply"
        assert audit_records[0].resource == f"assessment:{aid}"
        assert audit_records[0].outcome == "success"

    async def test_audit_log_fires_on_dry_run_failure_gate(self, caplog):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_marks_remediations_complete_on_apply above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": ["forbidden"]}
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].outcome == "dry-run-failed"

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

    async def test_not_registered_still_direct_applies_when_report_omitted(self):
        """Every pre-existing caller omits `report` -- must stay on the
        exact prior direct-apply behavior (2 calls: dry-run then real)."""
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
            mock_apply.return_value = {"applied": ["labels.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        assert mock_apply.call_count == 2


class TestExecuteConflictHandling:
    """`AutoMode.execute()`'s reaction to `kube.apply_yaml()`'s structured
    server-side-apply conflict result (surfaced through `apply_with_
    verification()`'s `conflicts` list): never silently forced, never
    lumped in with a generic partial failure -- always routed to a
    dedicated `cluster-conflict-review` gate."""

    def _conflict_result(self, applied=None):
        return {
            "applied": applied or [], "skipped": [], "errors": [],
            "conflicts": [{
                "path": "np.yaml", "error": "field-manager conflict",
                "details": [{"kind": "NetworkPolicy", "name": "test", "namespace": "default", "message": "conflict with kubectl"}],
            }],
        }

    async def test_dry_run_conflict_creates_conflict_review_gate(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_marks_remediations_complete_on_apply above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result()
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "conflict" in result["reason"].lower()
        mock_apply.assert_called_once()  # dry-run only; real apply never attempted
        gates = await raw.list_gates(status="pending")
        conflict_gates = [g for g in gates if g["gate_type"] == "cluster-conflict-review"]
        assert len(conflict_gates) == 1
        assert "force=True" in conflict_gates[0]["summary"]

    async def test_real_apply_conflict_after_clean_dry_run_creates_gate_not_partial_generic(self):
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_marks_remediations_complete_on_apply above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        call_results = [
            {"applied": [], "skipped": [], "errors": []},  # clean dry-run
            self._conflict_result(),  # real apply hits a conflict
        ]
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster", side_effect=call_results) as mock_apply:
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert mock_apply.call_count == 2
        assert result["action"] == "partial_failure"
        assert "conflict" in result["reason"].lower()
        gates = await raw.list_gates(status="pending")
        assert any(g["gate_type"] == "cluster-conflict-review" for g in gates)

    async def test_no_conflict_still_behaves_as_before(self):
        """Sanity check: a clean apply with no conflicts key issues (mocked
        return has no `conflicts` at all) must behave exactly as before this
        feature existed."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = await raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        # Real, parseable NetworkPolicy content -- see the comment on
        # test_marks_remediations_complete_on_apply above for why.
        files = [{
            "category": "sec", "path": "np.yaml", "description": "np",
            "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        }]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        gates = await raw.list_gates(status="pending")
        assert not any(g["gate_type"] == "cluster-conflict-review" for g in gates)


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
        ConfigMap in the same batch must still apply normally."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
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

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["cm.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        # The Secret must never have been handed to apply_manifests_to_cluster
        # at all, in either the dry-run or the real-apply call.
        for call in mock_apply.call_args_list:
            paths = {f["path"] for f in call.args[0]}
            assert "secret.yaml" not in paths
        assert mock_apply.call_count == 2  # dry-run + real apply, ConfigMap only
        assert result["action"] == "applied"
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

    async def test_mixed_batch_applies_cluster_config_and_gates_cicd_separately(self):
        """A single AutoMode batch mixing an ordinary ConfigMap with a
        CI/CD-shared-namespace manifest must split correctly: the ConfigMap
        applies directly, the CI/CD manifest gets its own admin-review gate
        -- both guards apply in the same call, not just in isolation."""
        s, raw = await make_async_store()
        await raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
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

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["cm.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        for call in mock_apply.call_args_list:
            paths = {f["path"] for f in call.args[0]}
            assert paths == {"cm.yaml"}
        assert result["action"] == "applied"
        gates = await raw.list_gates(status="pending")
        assert any(g["gate_type"] == "cluster-admin-review" for g in gates)


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
