"""Integration tests for POST /assessments/{id}/deliver -- the unified apply
flow's single entry point (docs/unified-apply-flow.md section (A)),
replacing the independent "Apply to Cluster" / "Create PR" buttons for
cluster/app config with one router-computed decision.
"""
from __future__ import annotations

import re
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.app import app
from conftest import make_report, make_store, prime_csrf


def _skill_file(path: str = "test-app-network-policy.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n",
        "description": "network policy",
    }


def _report_with_remediable_findings(repo_name: str = "test-app"):
    """Default make_report uses uncontracted category=test; Phase A gate
    refuses those. Deliver success paths need a remediable finding."""
    return make_report(
        repo_name=repo_name,
        scores=[DimensionScore(
            dimension="security", score=40, max_score=100,
            findings=[Finding(
                category="network", severity=Severity.high,
                description="missing network", recommendation="add NetworkPolicy",
            )],
        )],
    )


@pytest.fixture
async def deliver_client():
    store = await make_store()
    async_store = store
    report = _report_with_remediable_findings(repo_name="test-app")
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


class TestManualDeliverFindingGatePhaseA:
    """Manual /deliver must use the same Phase A finding_gate as
    auto_validate_and_deliver — no catalog-dump PRs from Onboard Results."""

    async def test_refuses_when_no_open_findings(self, _mock_kube):
        store = await make_store()
        report = make_report(
            repo_name="catalog-dump-app",
            scores=[DimensionScore(
                dimension="security", score=90, max_score=100, findings=[],
            )],
        )
        aid = await store.save(report)
        await store.save_onboarding(aid, [_skill_file()])
        await store.set_infra_repo_url(aid, "https://github.com/org/catalog-dump-gitops")

        with patch("agentit.portal.app.get_store", return_value=store), \
             patch("agentit.portal.routes.assessments.get_store", return_value=store), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=True,
            ) as client:
                await prime_csrf(client)
                resp = await client.post(
                    f"/assessments/{aid}/deliver", data={"dry_run": "false"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = unquote(resp.headers["location"])
        assert "error=" in location
        assert "No open findings" in location
        mock_commit.assert_not_called()
        assert await store.list_deliveries(aid) == []

    async def test_refuses_when_only_detect_only_findings(self, _mock_kube):
        store = await make_store()
        report = make_report(
            repo_name="detect-only-deliver-app",
            scores=[DimensionScore(
                dimension="security", score=50, max_score=100,
                findings=[
                    Finding(
                        category="license", severity=Severity.medium,
                        description="No LICENSE file found",
                        recommendation="Add a LICENSE",
                    ),
                    Finding(
                        category="secrets", severity=Severity.high,
                        description="Potential api_key found",
                        recommendation="Rotate and use a Secret",
                    ),
                ],
            )],
        )
        aid = await store.save(report)
        await store.save_onboarding(aid, [_skill_file()])
        await store.set_infra_repo_url(aid, "https://github.com/org/detect-only-gitops")

        with patch("agentit.portal.app.get_store", return_value=store), \
             patch("agentit.portal.routes.assessments.get_store", return_value=store), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=True,
            ) as client:
                await prime_csrf(client)
                resp = await client.post(
                    f"/assessments/{aid}/deliver", data={"dry_run": "false"},
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = unquote(resp.headers["location"])
        assert "error=" in location
        assert "detect_only" in location or "no_auto_pr" in location
        mock_commit.assert_not_called()

    async def test_allows_when_remediable_findings_exist(self, deliver_client, _mock_kube):
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
            resp = await client.post(
                f"/assessments/{aid}/deliver", data={"dry_run": "false"},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert "pull/4" in resp.headers["location"]
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()


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

        # Dry-run still persists apply_results (API/tests), but Onboard
        # Results no longer unlocks a Commit / Per-Agent CTA — Scan opens PRs.
        page = await client.get(f"/assessments/{aid}/onboard-results")
        assert page.status_code == 200
        assert 'data-action="apply"' not in page.text
        assert 'data-action="prs"' not in page.text
        assert "Commit & Open PR" not in page.text
        assert "Commit &amp; Open PR" not in page.text
        persisted = await store.get_apply_results(aid)
        assert persisted is not None
        assert persisted.get("dry_run") is True

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


class TestOnboardResultsScanOnlyNoManualDeliver:
    """Onboard Results must not offer Commit / Per-Agent CTAs. Deliver API
    routes remain for tests; the UI frames Scan + merge on GitHub."""

    async def test_no_manual_deliver_ctas_on_default_page(self, deliver_client):
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "Commit & Open PR" not in resp.text
        assert "Commit &amp; Open PR" not in resp.text
        assert "Per-Agent PRs" not in resp.text
        assert "Run Automatic Validation" not in resp.text
        assert 'data-action="apply"' not in resp.text
        assert 'data-action="prs"' not in resp.text
        assert 'data-action="apply-override"' not in resp.text
        assert "Download" in resp.text

    async def test_dry_run_flash_does_not_resurrect_commit_cta(self, deliver_client, _mock_kube):
        client, store, aid = deliver_client
        await store.set_infra_repo_url(aid, "https://github.com/org/infra-gitops")
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}):
            await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "true"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert 'data-action="apply"' not in resp.text
        assert "Commit & Open PR" not in resp.text
        assert "Commit &amp; Open PR" not in resp.text
        persisted = await store.get_apply_results(aid)
        assert persisted is not None
        assert persisted.get("dry_run") is True

    async def test_no_infra_repo_still_has_no_manual_deliver_cta(self, deliver_client):
        client, _store, aid = deliver_client
        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert 'data-action="apply"' not in resp.text
        assert 'data-action="apply-override"' not in resp.text
        assert "Commit & Open PR" not in resp.text

    async def test_gitops_dry_run_flash_and_persist_without_commit_cta(self, deliver_client, _mock_kube):
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
        assert "Commit & Open PR" not in html
        assert "Commit &amp; Open PR" not in html
        assert 'data-action="apply"' not in html
        assert "Start with a Dry Run" not in html

        clean = await client.get(f"/assessments/{aid}/onboard-results")
        assert clean.status_code == 200
        persisted = await store.get_apply_results(aid)
        assert persisted is not None
        assert persisted["dry_run"] is True
        assert not persisted.get("errors")
        assert "infra-repo-commit" in unquote(location)

    async def test_dry_run_summary_flash_still_renders(self, deliver_client):
        client, _store, aid = deliver_client
        resp = await client.get(
            f"/assessments/{aid}/onboard-results"
            "?dry_run=true&dry_run_summary=infra-repo-commit%20(1%20file(s))"
        )
        assert resp.status_code == 200
        assert "Dry run complete" in resp.text
        assert "choose how to deliver" not in resp.text.lower()
        assert 'data-action="apply"' not in resp.text

    async def test_onboard_results_fewer_stacked_alerts_after_gitops_dry_run(
        self, deliver_client, _mock_kube,
    ):
        """Primary status alert only; GitOps/shared-namespace callouts are secondary details."""
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

    async def test_real_delivery_page_has_no_manual_commit_cta(self, deliver_client, _mock_kube):
        client, _store, aid = deliver_client
        await client.post(f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False)

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert 'data-action="apply"' not in resp.text
        assert "Commit & Open PR" not in resp.text
