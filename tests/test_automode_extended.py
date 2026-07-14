"""Extended auto-mode tests — execute pipeline, LLM integration, edge cases."""

from __future__ import annotations

import logging

from unittest.mock import MagicMock, patch

from agentit.automode import AutoMode
from agentit.llm import LLMClient
from conftest import make_async_store, make_report


class TestExecuteAutoApply:
    async def test_auto_apply_with_safe_llm(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

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
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

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
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)
        raw.save_remediation(aid, "security", "Add NetworkPolicy")

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            await engine.execute(aid, [{"path": "np.yaml", "content": "x", "category": "sec", "description": "np"}],
                                  "default", "low", True, "test-app")

        rems = raw.list_remediations(aid)
        assert rems[0]["status"] == "completed"


class TestExecuteDryRunFirstAlwaysEnforced:
    """AutoMode's real, deliberately-preserved distinction from the manual
    "Apply to Cluster" route: it always dry-runs first, regardless of what
    the eventual real-apply outcome would be, and never skips straight to a
    real apply."""

    async def test_dry_run_called_before_real_apply(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

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
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Adds ConfigMap",
        }
        files = [{"category": "cost", "path": "labels.yaml", "content": "x", "description": "labels"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["labels.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            with caplog.at_level(logging.INFO, logger="agentit.audit"):
                result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        assert len(audit_records) == 1
        assert audit_records[0].actor == "auto-mode"
        assert audit_records[0].action == "auto-apply"
        assert audit_records[0].resource == f"assessment:{aid}"
        assert audit_records[0].outcome == "success"

    async def test_audit_log_fires_on_dry_run_failure_gate(self, caplog):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {
            "is_destructive": False, "confidence": 0.95, "reason": "Safe",
        }
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

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
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

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
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = raw.save(report)

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

        gates = raw.list_gates(status="pending")
        assert len(gates) == 1
        assert gates[0]["gate_type"] == "gitops-pr-pending"
        assert "pull/7" in gates[0]["summary"]
        assert "never auto-merge" in gates[0]["summary"]

    async def test_not_registered_still_direct_applies_when_report_omitted(self):
        """Every pre-existing caller omits `report` -- must stay on the
        exact prior direct-apply behavior (2 calls: dry-run then real)."""
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

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
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = self._conflict_result()
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "gated"
        assert "conflict" in result["reason"].lower()
        mock_apply.assert_called_once()  # dry-run only; real apply never attempted
        gates = raw.list_gates(status="pending")
        conflict_gates = [g for g in gates if g["gate_type"] == "cluster-conflict-review"]
        assert len(conflict_gates) == 1
        assert "force=True" in conflict_gates[0]["summary"]

    async def test_real_apply_conflict_after_clean_dry_run_creates_gate_not_partial_generic(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

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
        gates = raw.list_gates(status="pending")
        assert any(g["gate_type"] == "cluster-conflict-review" for g in gates)

    async def test_no_conflict_still_behaves_as_before(self):
        """Sanity check: a clean apply with no conflicts key issues (mocked
        return has no `conflicts` at all) must behave exactly as before this
        feature existed."""
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

        llm = MagicMock()
        llm.classify_action.return_value = {"is_destructive": False, "confidence": 0.95, "reason": "Safe"}
        files = [{"category": "sec", "path": "np.yaml", "content": "x", "description": "np"}]

        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["np.yaml"], "skipped": [], "errors": []}
            engine = AutoMode(store=s, llm_client=llm)
            result = await engine.execute(aid, files, "default", "low", True, "test-app")

        assert result["action"] == "applied"
        gates = raw.list_gates(status="pending")
        assert not any(g["gate_type"] == "cluster-conflict-review" for g in gates)


class TestExecuteWithPublisher:
    async def test_publishes_events(self):
        s, raw = make_async_store()
        raw.set_setting("auto_mode", "true")
        report = make_report(criticality="low", summary="test")
        aid = raw.save(report)

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
