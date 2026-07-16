"""Integration tests for the collapsed gate-approve flow
(routes/gates.py::resolve_gate) -- confirms gate-approve now funnels through
the unified delivery router (finding #1 in docs/unified-apply-flow.md: a
gate approval previously called raw `apply_manifests_to_cluster()` directly,
with no audit trail and no GitOps awareness), and the two new gate types
(`cluster-admin-review`, `gitops-pr-pending`) resolve correctly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


def _cluster_config_file() -> dict:
    return {
        "category": "skills",
        "path": "test-app-network-policy.yaml",
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


def _cicd_file() -> dict:
    return {
        "category": "skills",
        "path": "pipeline.yaml",
        "content": (
            "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
            "metadata:\n  name: build\n  namespace: openshift-pipelines\n"
        ),
        "description": "tekton pipeline",
    }


@pytest.fixture
async def gate_client():
    store = await make_store()
    async_store = store
    report = make_report(repo_name="test-app")
    aid = await store.save(report)
    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.gates.get_store", return_value=async_store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store, aid


@pytest.fixture(autouse=True)
def _mock_kube():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


class TestResolveGateFunnelsThroughRouter:
    async def test_approve_with_cluster_config_files_applies_directly_when_not_registered(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(aid, "deploy", "Approve deployment")

        resp = await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "approved", "resolved_by": "tester"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "gate_approved=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["mechanism"] == "cluster_config:direct-apply"

    async def test_approve_audits_the_delivery(self, gate_client, _mock_kube, caplog):
        import logging
        client, store, aid = gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(aid, "deploy", "Approve deployment")

        with caplog.at_level(logging.INFO, logger="agentit.audit"):
            await client.post(
                f"/gates/{gate_id}/resolve",
                data={"status": "approved", "resolved_by": "tester"},
                follow_redirects=False,
            )

        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        # One for the generic "gate-approved" audit line, one for the
        # delivery itself (closing the pre-existing gap: gate-approve had
        # zero audit log entry for the apply itself).
        deliver_records = [r for r in audit_records if r.action == "deliver-apply"]
        assert len(deliver_records) == 1
        assert deliver_records[0].outcome == "success"


class TestClusterAdminReviewGate:
    async def test_approve_applies_directly_into_operator_namespace(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        await store.save_onboarding(aid, [_cicd_file()])
        gate_id = await store.create_gate(aid, "cluster-admin-review", "Needs elevated RBAC")

        resp = await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "approved", "resolved_by": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "gate_approved=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()
        # The manifest's own declared operator namespace must be preserved,
        # not rewritten to the app's namespace.
        call_args = _mock_kube.apply_yaml.call_args
        assert "openshift-pipelines" in call_args[0][0]

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1


class TestClusterConflictReviewGate:
    """Approving a `cluster-conflict-review` gate is the ONE code path in
    the app that ever passes `force=True` down to `kube.apply_yaml()` --
    only reachable after a human has explicitly reviewed a field-manager
    conflict and chosen to seize ownership."""

    async def test_approve_force_reapplies_cluster_config_files(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(
            aid, "cluster-conflict-review",
            "1 manifest(s) hit a server-side-apply field-manager conflict. "
            "Approving this gate re-applies with force=True, seizing ownership.",
        )

        resp = await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "approved", "resolved_by": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "gate_approved=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()
        assert _mock_kube.apply_yaml.call_args.kwargs["force"] is True

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1

    async def test_missing_onboarding_files_leaves_gate_pending_with_error(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        gate_id = await store.create_gate(aid, "cluster-conflict-review", "Conflict detected")

        resp = await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "approved", "resolved_by": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_not_called()
        assert len(await store.list_gates(status="pending")) == 1
        assert len(await store.list_gates(status="approved")) == 0

    async def test_forced_apply_failure_leaves_gate_pending(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(aid, "cluster-conflict-review", "Conflict detected")
        _mock_kube.apply_yaml.side_effect = RuntimeError("cluster unreachable")

        resp = await client.post(
            f"/gates/{gate_id}/resolve",
            data={"status": "approved", "resolved_by": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert len(await store.list_gates(status="pending")) == 1


class TestGitopsPrPendingGate:
    async def test_approve_merges_the_pr_not_reapply(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        gate_id = await store.create_gate(
            aid, "gitops-pr-pending",
            "AgentIT will: commit to `https://github.com/org/infra-gitops` and open a PR. "
            "PR opened: https://github.com/org/infra-gitops/pull/9. "
            "Approving this gate merges the PR -- AgentIT never auto-merges.",
        )

        with patch("agentit.portal.github_pr.merge_pr") as mock_merge:
            mock_merge.return_value = {"merged": True, "sha": "abc123"}
            resp = await client.post(
                f"/gates/{gate_id}/resolve",
                data={"status": "approved", "resolved_by": "tester"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "pull/9" in resp.headers["location"]
        mock_merge.assert_called_once_with("https://github.com/org/infra-gitops/pull/9")
        _mock_kube.apply_yaml.assert_not_called()

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1

    async def test_merge_failure_leaves_gate_pending(self, gate_client, _mock_kube):
        client, store, aid = gate_client
        gate_id = await store.create_gate(
            aid, "gitops-pr-pending",
            "PR opened: https://github.com/org/infra-gitops/pull/9. Approving merges the PR.",
        )

        with patch("agentit.portal.github_pr.merge_pr") as mock_merge:
            mock_merge.return_value = {"error": "merge conflict"}
            resp = await client.post(
                f"/gates/{gate_id}/resolve",
                data={"status": "approved", "resolved_by": "tester"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert len(await store.list_gates(status="pending")) == 1
        assert len(await store.list_gates(status="approved")) == 0
