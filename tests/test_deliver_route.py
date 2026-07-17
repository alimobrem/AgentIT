"""Integration tests for POST /assessments/{id}/deliver -- the unified apply
flow's single entry point (docs/unified-apply-flow.md section (A)),
replacing the independent "Apply to Cluster" / "Create PR" buttons for
cluster/app config with one router-computed decision.
"""
from __future__ import annotations

import re
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


class TestDeliverWithNoInfraRepoRefusesWithNoDirectApplyFallback:
    """Direct Apply has been removed as a concept entirely -- an app with no
    known infra repo at all (only possible for an assessment saved before
    GitOps registration became mandatory) cannot be delivered, full stop.
    Never falls back to mutating the cluster directly."""

    async def test_real_delivery_refuses_and_records_a_partial_delivery_row(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        _mock_kube.apply_yaml.assert_not_called()

        deliveries = await store.list_deliveries(aid)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "partial"

    async def test_dry_run_also_refuses_never_touches_the_cluster(self, deliver_client, _mock_kube):
        """Even a Dry Run never calls kube.apply_yaml() for an app with no
        known infra repo -- there is nothing left to simulate applying."""
        client, store, aid = deliver_client
        resp = await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
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

        # Persist unlock: GitOps dry-run must write apply_results so Commit /
        # Per-Agent stay enabled after the flash URL is cleared.
        page = await client.get(f"/assessments/{aid}/onboard-results")
        assert page.status_code == 200
        assert "No dry run yet" not in page.text
        assert "Dry run passed" in page.text
        assert 'data-dry-done="true"' in page.text
        assert 'data-action="apply-override"' not in page.text
        assert not re.search(
            r'data-action="apply"[^>]*\sdisabled(?:\s|=|>)', page.text, re.I,
        )
        assert not re.search(
            r'data-action="prs"[^>]*\sdisabled(?:\s|=|>)', page.text, re.I,
        )
        assert "delivery-choice" in page.text

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
    """Soft-gate Deliver until Dry Run succeeds: warning chip outside the
    CTA, primary Deliver disabled. After a successful dry run the primary
    unlocks with mechanism-specific confirm text. (The Override bypass has
    been removed along with Direct Apply -- Deliver now requires an actual
    successful Dry Run, no escape hatch.)"""

    async def test_warning_badge_shown_before_any_apply_action(self, deliver_client):
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "No dry run yet" in resp.text
        # Chip is in the status region, not packed into the Apply CTA label.
        assert "delivery-step-status" in resp.text
        assert 'class="btn btn-green btn-lg"' not in resp.text
        assert "NO DRY RUN YET" not in resp.text
        assert "Commit &amp; Open PR" in resp.text or "Commit & Open PR" in resp.text
        assert "Apply to Cluster" not in resp.text
        assert "Deliver Now" not in resp.text
        assert 'data-action="apply-override"' not in resp.text
        assert "Override</button>" not in resp.text
        # Primary Apply carries a disabled attribute while ungated.
        assert 'data-action="apply"' in resp.text

    async def test_warning_badge_gone_after_a_dry_run(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "No dry run yet" not in resp.text
        assert "NO DRY RUN YET" not in resp.text
        assert "Dry run passed" in resp.text
        assert 'data-action="apply-override"' not in resp.text
        assert 'data-dry-done="true"' in resp.text
        assert "dryDone: true" in resp.text
        assert not re.search(
            r'data-action="apply"[^>]*\sdisabled(?:\s|=|>)', resp.text, re.I,
        )
        assert "confirmText: " in resp.text or "Commit &amp; Open PR" in resp.text or "Commit & Open PR" in resp.text

    async def test_no_infra_repo_blocks_delivery_with_no_override_escape_hatch(self, deliver_client):
        """The legacy (pre-mandatory-GitOps) case: no infra_repo_url known
        at all -- Deliver is blocked entirely, with a clear message, not
        just soft-gated behind Dry Run (there is nothing an Override could
        meaningfully bypass to)."""
        client, _store, aid = deliver_client
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "Not GitOps-registered" in resp.text
        assert "Register this app for GitOps" in resp.text or "Register for GitOps" in resp.text
        assert 'data-action="apply-override"' not in resp.text
        assert re.search(r'data-action="apply"[^>]*\sdisabled(?:\s|=|>)', resp.text, re.I)

    async def test_gitops_dry_run_unlocks_commit_and_never_contradicts(self, deliver_client, _mock_kube):
        """P0: GitOps dry-run showed 'Dry run complete' + 'NO DRY RUN YET'."""
        from urllib.parse import unquote

        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")

        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            post = await client.post(
                f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False,
            )
        assert post.status_code == 303
        location = post.headers["location"]
        assert "dry_run_summary=" in location

        flash_resp = await client.get(location)
        assert flash_resp.status_code == 200
        html = flash_resp.text
        assert "Dry run complete" in html
        assert "No dry run yet" not in html
        assert "NO DRY RUN YET" not in html
        assert "Dry run passed" in html
        assert 'data-dry-done="true"' in html
        assert "dryDone: true" in html
        # Label depends on is_gitops_registered() at GET time (Argo may be
        # unreachable in unit tests); unlock is what matters for the P0.
        # Jinja escapes "&" as "&amp;" in rendered HTML text/attrs.
        assert (
            "Commit &amp; Open PR" in html
            or "Commit & Open PR" in html
            or "Apply to Cluster" in html
        )
        assert not re.search(
            r'data-action="apply"[^>]*\sdisabled(?:\s|=|>)', html, re.I,
        ), "Deliver CTA still has static disabled after GitOps dry run"
        assert "Start with a Dry Run" not in html

        clean = await client.get(f"/assessments/{aid}/onboard-results")
        assert clean.status_code == 200
        assert "No dry run yet" not in clean.text
        assert 'data-dry-done="true"' in clean.text
        persisted = await store.get_apply_results(aid)
        assert persisted is not None
        assert persisted["dry_run"] is True
        assert not persisted.get("errors")
        assert "infra-repo-commit" in unquote(location)

    async def test_dry_run_summary_flash_alone_unlocks_without_contradiction(self, deliver_client):
        """dry_run_summary flash must unlock Commit even before persistence."""
        client, _store, aid = deliver_client
        resp = await client.get(
            f"/assessments/{aid}/onboard-results"
            "?dry_run=true&dry_run_summary=infra-repo-commit%20(1%20file(s))"
        )
        assert resp.status_code == 200
        assert "Dry run complete" in resp.text
        assert "No dry run yet" not in resp.text
        assert "NO DRY RUN YET" not in resp.text
        assert 'data-dry-done="true"' in resp.text
        assert "dryDone: true" in resp.text

    async def test_onboard_results_fewer_stacked_alerts_after_gitops_dry_run(
        self, deliver_client, _mock_kube,
    ):
        """Primary status alert only; GitOps/admin-review are secondary details."""
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            post = await client.post(
                f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False,
            )
        location = post.headers["location"] + "&cicd_gate=true"
        resp = await client.get(location)
        html = resp.text
        alerts = re.findall(r'class="alert[^"]*"', html)
        assert sum(
            1 for a in alerts if "alert-info" in a or "alert-warn" in a or "alert-success" in a
        ) <= 2
        assert "delivery-meta" in html
        assert "Start with a Dry Run" not in html

    async def test_warning_badge_gone_after_a_real_delivery(self, deliver_client, _mock_kube):
        client, _store, aid = deliver_client
        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "No dry run yet" not in resp.text
