"""Integration tests for POST /assessments/{id}/deliver -- the unified apply
flow's single entry point (docs/unified-apply-flow.md section (A)),
replacing the independent "Apply to Cluster" / "Create PR" buttons for
cluster/app config with one router-computed decision.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


def _skill_file(path: str = "test-app-network-policy.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


@pytest.fixture
async def deliver_client():
    store = await make_store()
    async_store = store
    report = make_report(repo_name="test-app")
    assessment_id = await store.save(report)
    await store.save_onboarding(assessment_id, [_skill_file()])

    with patch("agentit.portal.app.get_store", return_value=async_store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=async_store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store, assessment_id


@pytest.fixture(autouse=True)
def _mock_kube():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


class TestDeliverNotRegisteredAppliesDirectly:
    async def test_real_delivery_applies_and_records_delivery_row(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "applied=1" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_called_once()

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["mechanism"] == "cluster_config:direct-apply"
        assert deliveries[0]["status"] == "delivered"

    async def test_dry_run_never_calls_apply_yaml(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "dry_run=true" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_not_called()


class TestDeliverRegisteredCommitsToInfraRepo:
    async def test_real_delivery_commits_and_opens_pr_not_direct_apply(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset") as mock_ensure:
            mock_commit.return_value = {
                "pr_url": "https://github.com/org/infra-gitops/pull/4",
                "commit_url": "https://github.com/org/infra-gitops/commit/cafebabe",
                "files_committed": 1,
            }
            resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        assert resp.status_code == 303
        assert "pull/4" in resp.headers["location"]
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()
        _mock_kube.apply_yaml.assert_not_called()

    async def test_dry_run_of_gitops_commit_surfaces_a_preview_not_nothing(self, deliver_client, _mock_kube):
        """Regression test for the live bug report "commit and open PR
        doesn't do anything": for a GitOps-registered app, clicking "Dry
        Run" routed cluster_config to ``MECHANISM_INFRA_REPO_COMMIT``'s
        dry-run branch, which only ever returned ``{"dry_run": True,
        "files": [...]}`` -- the ``deliver()`` route only ever looked at
        ``cluster_outcome.get("pr_url")``/``"applied"``, so this dry-run
        outcome added *zero* redirect params beyond ``delivery_id``/
        ``dry_run=true``, and the reloaded page showed no alert, no updated
        step-guide, nothing -- indistinguishable from the button doing
        nothing at all.
        """
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "dry_run=true" in location
        assert "dry_run_summary=" in location, (
            "dry-run through the GitOps-commit path produced no visible "
            "preview in the redirect -- this is the 'doesn't do anything' bug"
        )
        assert "infra-repo-commit" in location

    async def test_failed_infra_repo_commit_surfaces_a_visible_error(self, deliver_client, _mock_kube):
        """A real (non-dry-run) ``commit_to_infra_repo()`` failure returns
        ``{"error": ...}`` rather than raising (see github_pr.py) -- the old
        ``deliver()`` redirect logic only ever inspected ``pr_url``/
        ``applied`` on the cluster_config outcome, so this error was
        silently dropped: the redirect carried only ``delivery_id``/
        ``dry_run=false``, no ``error`` param, and the page looked
        unchanged -- the exact "doesn't do anything" symptom, but for a
        real failed delivery rather than a dry run.
        """
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            mock_commit.return_value = {"error": "GitHub API error: 404 Not Found"}
            resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "error=" in location, (
            "a failed commit_to_infra_repo() was silently dropped from the "
            "redirect -- this is the 'doesn't do anything' bug"
        )
        assert "Not+Found" in location or "Not%20Found" in location


class TestOnboardResultsWarnsBeforeDryRun:
    """The page's own banner recommends dry-run-then-deliver, but nothing
    enforced or even visually hinted at this -- a user could click Deliver
    with zero friction, having never dry-run. A visible warning badge next
    to the Deliver button must appear until a dry run (or a real delivery)
    has actually happened for this assessment."""

    async def test_warning_badge_shown_before_any_apply_action(self, deliver_client):
        client, _store, aid = deliver_client
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "No dry run yet" in resp.text

    async def test_warning_badge_gone_after_a_dry_run(self, deliver_client, _mock_kube):
        client, _store, aid = deliver_client
        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "No dry run yet" not in resp.text

    async def test_warning_badge_gone_after_a_real_delivery(self, deliver_client, _mock_kube):
        client, _store, aid = deliver_client
        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "No dry run yet" not in resp.text
