"""Integration tests for POST /assessments/{id}/deliver -- the unified apply
flow's single entry point (docs/unified-apply-flow.md section (A)),
replacing the independent "Apply to Cluster" / "Create PR" buttons for
cluster/app config with one router-computed decision.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentit.portal.app import app
from agentit.portal.store_factory import AsyncSQLiteStore
from conftest import make_report, make_store, prime_csrf


def _skill_file(path: str = "test-app-network-policy.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


@pytest.fixture
def deliver_client():
    store = make_store()
    async_store = AsyncSQLiteStore.wrap(store)
    report = make_report(repo_name="test-app")
    assessment_id = store.save(report)
    store.save_onboarding(assessment_id, [_skill_file()])

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store):
        client = TestClient(app)
        prime_csrf(client)
        yield client, store, assessment_id


@pytest.fixture(autouse=True)
def _mock_kube():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


class TestDeliverNotRegisteredAppliesDirectly:
    def test_real_delivery_applies_and_records_delivery_row(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        resp = client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "applied=1" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()

        deliveries = store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["mechanism"] == "cluster_config:direct-apply"
        assert deliveries[0]["status"] == "delivered"

    def test_dry_run_never_calls_apply_yaml(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        resp = client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "dry_run=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_not_called()


class TestDeliverRegisteredCommitsToInfraRepo:
    def test_real_delivery_commits_and_opens_pr_not_direct_apply(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        report = store.get(aid)
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        store._conn.execute(
            "UPDATE assessments SET report_json = ? WHERE id = ?",
            (report.model_dump_json(), aid),
        )
        store._conn.commit()

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset") as mock_ensure:
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/infra-gitops/pull/4",
                "commit_url": "https://github.com/org/infra-gitops/commit/cafebabe",
                "files_committed": 1,
            }
            resp = client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        assert resp.status_code == 303
        assert "pull/4" in resp.headers["location"]
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()
        _mock_kube.apply_yaml.assert_not_called()
