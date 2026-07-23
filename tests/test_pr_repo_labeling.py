"""Every PR-reference surface (Delivery History, the Ledger's delivery
cards, the deliver-flow flash alert, and a GitOps-PR gate merge) must
label which of an app's two repos (its own code repo, ``report.repo_url``,
vs. its GitOps repo, ``report.infra_repo_url``) a given PR actually
targets, traced from ``delivery.py``'s real mechanism-to-repo mapping
(``repo_kind_for_mechanism()``) rather than guessed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentit.models import DimensionScore, Finding, Severity
from agentit.portal.app import app
from agentit.portal.delivery import repo_kind_for_mechanism
from conftest import make_report, make_store, prime_csrf


def _report_with_remediable_findings(repo_name: str, category: str = "network"):
    return make_report(
        repo_name=repo_name,
        scores=[DimensionScore(
            dimension="security", score=40, max_score=100,
            findings=[Finding(
                category=category, severity=Severity.high,
                description=f"missing {category}", recommendation=f"add {category}",
            )],
        )],
    )


@pytest.fixture(autouse=True)
def _mock_kube():
    """``is_gitops_registered()``/Fleet's Argo enrichment both call into
    ``kube``; stub them so tests aren't at the mercy of whatever cluster
    KUBECONFIG happens to point to (mirrors test_ui_redesign.py's fixture
    of the same name)."""
    with patch("agentit.portal.cluster_apply.kube") as mock_apply_kube, \
         patch("agentit.portal.delivery.kube") as mock_delivery_kube, \
         patch("agentit.kube.list_custom_resources") as mock_list:
        mock_apply_kube.namespace_exists.return_value = True
        mock_apply_kube.get_api_resources.return_value = set()
        mock_apply_kube.apply_yaml.return_value = {"applied": True, "error": None}
        mock_delivery_kube.get_custom_resource.side_effect = Exception("no cluster in tests")
        mock_list.return_value = []
        yield {"delivery": mock_delivery_kube, "list_custom_resources": mock_list}


@pytest.fixture
async def ui_client():
    store = await make_store()
    with patch("agentit.portal.app.get_store", return_value=store), \
         patch("agentit.portal.routes.assessments.get_store", return_value=store), \
         patch("agentit.portal.routes.fleet.get_store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
            await prime_csrf(client)
            yield client, store


def _skill_file(path: str = "netpol.yaml") -> dict:
    return {
        "category": "skills",
        "path": path,
        "content": (
            "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            "metadata:\n  name: test\n"
            "spec:\n  podSelector: {}\n  policyTypes:\n    - Ingress\n"
        ),
        "description": "network policy",
        "skill_name": "network-policy",
        "finding_addressed": "network",
    }


def _source_patch_file() -> dict:
    return {
        "category": "codechange",
        "path": "patch-01-Dockerfile",
        "content": (
            "FROM registry.access.redhat.com/ubi9/python-312:1\n"
            "USER 1001\n"
        ),
        "description": "Dockerfile pin",
        "target_path": "Dockerfile",
        "skill_name": "containerfile",
        "finding_addressed": "container",
    }


class TestRepoKindForMechanism:
    """Unit coverage for the traced mechanism-to-repo mapping itself."""

    def test_infra_repo_commit_is_gitops(self):
        assert repo_kind_for_mechanism("infra-repo-commit") == "gitops"

    def test_source_repo_pr_is_code(self):
        assert repo_kind_for_mechanism("source-repo-pr") == "code"

    def test_app_repo_pr_is_code(self):
        assert repo_kind_for_mechanism("app-repo-pr") == "code"

    def test_direct_apply_has_no_repo_target(self):
        assert repo_kind_for_mechanism("direct-apply") == ""

    def test_cluster_admin_review_gate_has_no_repo_target(self):
        assert repo_kind_for_mechanism("cluster-admin-review-gate") == ""


class TestDeliveryHistoryMechanismRepoLabels:
    """onboard_results.html's Delivery History table decodes ``mechanism``
    via ``humanize_mechanism`` (ledger.py's ``humanize_delivery_mechanism``)
    -- confirm it now names which repo each PR-opening mechanism targets."""

    async def test_labels_code_repo_for_source_repo_pr(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="delivery-source-pr-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": ["patch-01-Dockerfile"]},
            "source_patch:source-repo-pr",
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "PR opened against the code repo" in resp.text
        assert "source_patch:source-repo-pr" not in resp.text

    async def test_labels_code_repo_for_app_repo_pr(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="delivery-app-repo-pr-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_skill_file()])
        await store.create_delivery(
            aid, report.repo_name, {"manifest_at_rest": ["renovate.json"]},
            "manifest_at_rest:app-repo-pr",
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "PR opened against the code repo" in resp.text

    async def test_labels_gitops_repo_for_infra_repo_commit(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="delivery-infra-pr-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_skill_file()])
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": ["netpol.yaml"]},
            "cluster_config:infra-repo-commit",
        )

        resp = await client.get(f"/assessments/{aid}/onboard-results")
        assert resp.status_code == 200
        assert "PR opened against the GitOps repo" in resp.text
        assert "cluster_config:infra-repo-commit" not in resp.text


class TestLedgerMechanismRepoLabels:
    """The Ledger's delivery cards (card type F, ``ledger.py``'s
    ``_delivery_card()``) share the exact same ``humanize_delivery_mechanism``
    decode -- confirmed here via the chain replay view (``ledger_chain.html``),
    which renders ``card.mechanism | humanize_mechanism`` directly."""

    async def test_chain_view_labels_code_repo_pr(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="ledger-code-repo-app")
        aid = await store.save(report)
        await store.create_delivery(
            aid, report.repo_name, {"source_patch": ["a.py"]}, "source_patch:source-repo-pr",
        )

        resp = await client.get(f"/ledger/chain/{aid}")
        assert resp.status_code == 200
        assert "PR opened against the code repo" in resp.text

    async def test_chain_view_labels_gitops_repo_pr(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="ledger-gitops-repo-app")
        aid = await store.save(report)
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": ["a.yaml"]}, "cluster_config:infra-repo-commit",
        )

        resp = await client.get(f"/ledger/chain/{aid}")
        assert resp.status_code == 200
        assert "PR opened against the GitOps repo" in resp.text


class TestDeliverFlowFlashRepoLabels:
    """The /deliver route's single-``pr_url`` flash alert previously showed
    a bare "PR created: <link>" for *any* PR-opening mechanism -- now it
    carries ``pr_url_repo`` (traced from the real mechanism that produced
    the winning pr_url) so onboard_results.html can label it."""

    async def test_gitops_commit_flash_labels_gitops_repo(self):
        store = await make_store()
        report = _report_with_remediable_findings("deliver-flash-gitops-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_skill_file()])
        await store.set_infra_repo_url(aid, "https://github.com/org/deliver-flash-infra-gitops")

        with patch("agentit.portal.app.get_store", return_value=store), \
             patch("agentit.portal.routes.assessments.get_store", return_value=store):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
                await prime_csrf(client)
                with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
                     patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
                     patch("agentit.portal.github_pr.ensure_applicationset"):
                    mock_commit.return_value = {
                        "pr_url": "https://github.com/org/deliver-flash-infra-gitops/pull/4",
                        "commit_url": "https://github.com/org/deliver-flash-infra-gitops/commit/cafebabe",
                        "files_committed": 1,
                    }
                    post = await client.post(
                        f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False,
                    )

                assert post.status_code == 303
                location = post.headers["location"]
                assert "pr_url_repo=gitops" in location

                page = await client.get(location)
        assert page.status_code == 200
        assert "PR opened against the GitOps repo" in page.text

    async def test_source_repo_pr_flash_labels_code_repo(self):
        store = await make_store()
        report = _report_with_remediable_findings("deliver-flash-code-app", category="container")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_source_patch_file()])

        with patch("agentit.portal.app.get_store", return_value=store), \
             patch("agentit.portal.routes.assessments.get_store", return_value=store):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True) as client:
                await prime_csrf(client)
                with patch("agentit.portal.github_pr.create_source_patch_pr") as mock_pr:
                    mock_pr.return_value = {
                        "pr_url": "https://github.com/org/deliver-flash-code-app/pull/9",
                        "branch": "agentit/codechange", "files_committed": 1,
                    }
                    post = await client.post(
                        f"/assessments/{aid}/deliver", data={"dry_run": "false"}, follow_redirects=False,
                    )

                assert post.status_code == 303
                location = post.headers["location"]
                assert "pr_url_repo=code" in location

                page = await client.get(location)
        assert page.status_code == 200
        assert "PR opened against the code repo" in page.text


class TestGitopsPrMergeRedirect:
    """A `cluster_config`-delivered PR (via the GitOps infra repo) merges
    through the same real `/prs/merge` action every other PR-backed
    category uses now (the `gates` table/generic gate-resolution
    machinery has been removed entirely, 2026-07-19) -- confirm merging it
    lands back on that app's own Ledger tab, where the PR's real GitOps-
    repo link is already visible in the PR history table."""

    async def test_merging_a_gitops_delivered_pr_redirects_to_own_ledger_tab(self, ui_client):
        client, store = ui_client
        report = make_report(repo_name="gate-merge-gitops-app")
        aid = await store.save(report)
        await store.save_onboarding(aid, [_skill_file()])
        pr_url = "https://github.com/org/gate-merge-infra-gitops/pull/3"
        await store.create_delivery(
            aid, report.repo_name, {"cluster_config": 1}, mechanism="cluster_config:infra-repo-commit",
            status="delivered", details={"outcomes": {"cluster_config": {"pr_url": pr_url}}},
        )

        with patch("agentit.portal.github_pr.merge_pr") as mock_merge:
            mock_merge.return_value = {"merged": True, "sha": "abc123"}
            resp = await client.post(
                "/prs/merge",
                data={"pr_url": pr_url, "assessment_id": aid},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        assert f"/assessments/{aid}?tab=ledger" in resp.headers["location"]

        with patch(
            "agentit.portal.github_pr.get_pr_status",
            return_value={"state": "merged", "html_url": pr_url, "title": "fix", "merged_at": "2026-01-05T00:00:00"},
        ):
            page = await client.get(resp.headers["location"])
        assert page.status_code == 200
        assert pr_url in page.text
