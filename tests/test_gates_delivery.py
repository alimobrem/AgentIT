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


@pytest.fixture
async def gitops_gate_client():
    """Same as ``gate_client`` but with a known ``infra_repo_url`` -- Direct
    Apply has been removed as a concept entirely, so any test exercising the
    generic gate-approve path through ``route_and_deliver()`` needs a known
    infra repo or delivery refuses outright (see
    ``resolve_cluster_config_mechanism()``)."""
    store = await make_store()
    async_store = store
    report = make_report(repo_name="test-app")
    report.infra_repo_url = "https://github.com/org/infra-gitops"
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
    async def test_approve_with_cluster_config_files_commits_to_infra_repo(self, gitops_gate_client, _mock_kube):
        """Direct Apply has been removed as a concept entirely -- a generic
        gate approval for cluster-config files now always resolves to a
        GitOps commit+PR (given a known infra_repo_url), never a direct
        apply, so this never touches ``kube.apply_yaml`` at all."""
        client, store, aid = gitops_gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(aid, "deploy", "Approve deployment")

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            resp = await client.post(
                f"/gates/{gate_id}/resolve",
                data={"status": "approved", "resolved_by": "tester"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "gate_approved=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_not_called()
        mock_commit.assert_called_once()

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["mechanism"] == "cluster_config:infra-repo-commit"

        # The successful commit also opens a gitops-pr-pending gate --
        # a human still merges, AgentIT still never auto-merges.
        pending = await store.list_gates(status="pending")
        assert any(g["gate_type"] == "gitops-pr-pending" for g in pending)

    async def test_no_infra_repo_refuses_with_no_direct_apply_fallback(self, gate_client, _mock_kube):
        """The legacy (pre-mandatory-GitOps) case: no infra_repo_url known
        at all -- refuses outright rather than falling back to a direct
        apply."""
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
        _mock_kube.apply_yaml.assert_not_called()

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "partial"

    async def test_concurrent_resolve_requests_deliver_only_once(self, gitops_gate_client, _mock_kube):
        """Regression guard for the check-then-act gate-resolution race
        (Priority 1b): two genuinely concurrent resolve requests for the
        SAME pending gate must not both perform the delivery. Before
        the fix, the route read the gate as `pending`, ran the side
        effect, and only afterward called the atomic `resolve_gate()`
        status-flip -- so two near-simultaneous requests could both read
        `pending` and both deliver. `list_gates("pending")` (the route's
        own initial read) is slowed down here so both concurrent requests
        genuinely observe the gate as still-pending before either one
        reaches its `resolve_gate()` claim -- reproducing the exact
        "read pending, then race to act" window the bug describes,
        rather than one request simply finishing before the other starts.
        """
        import asyncio

        from agentit.portal.store import AssessmentStore

        client, store, aid = gitops_gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(aid, "deploy", "Approve deployment")

        orig_list_gates = AssessmentStore.list_gates

        async def _slow_list_gates(self, status="pending"):
            result = await orig_list_gates(self, status)
            if status == "pending":
                await asyncio.sleep(0.2)
            return result

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             patch.object(AssessmentStore, "list_gates", _slow_list_gates):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            resp1, resp2 = await asyncio.gather(
                client.post(f"/gates/{gate_id}/resolve", data={"status": "approved", "resolved_by": "tester"}, follow_redirects=False),
                client.post(f"/gates/{gate_id}/resolve", data={"status": "approved", "resolved_by": "tester"}, follow_redirects=False),
            )

        assert mock_commit.call_count == 1
        locations = [resp1.headers["location"], resp2.headers["location"]]
        assert sum(1 for l in locations if "error=" in l) == 1
        assert sum(1 for l in locations if "gate_approved=true" in l) == 1

        approved = await store.list_gates(status="approved")
        assert len(approved) == 1

    async def test_approve_audits_the_delivery(self, gitops_gate_client, _mock_kube, caplog):
        import logging
        client, store, aid = gitops_gate_client
        await store.save_onboarding(aid, [_cluster_config_file()])
        gate_id = await store.create_gate(aid, "deploy", "Approve deployment")

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset"), \
             caplog.at_level(logging.INFO, logger="agentit.audit"):
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            await client.post(
                f"/gates/{gate_id}/resolve",
                data={"status": "approved", "resolved_by": "tester"},
                follow_redirects=False,
            )

        audit_records = [r for r in caplog.records if getattr(r, "audit", False)]
        # One for the generic "gate-approved" audit line, one for the
        # delivery itself (closing the pre-existing gap: gate-approve had
        # zero audit log entry for the commit+PR itself).
        deliver_records = [r for r in audit_records if r.action == "deliver"]
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
