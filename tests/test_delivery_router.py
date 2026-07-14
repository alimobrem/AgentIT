"""Tests for the unified apply flow's router (portal/delivery.py) --
classification into docs/unified-apply-flow.md's taxonomy, GitOps
registration detection, and end-to-end routing through
``route_and_deliver()`` for each category.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.portal.delivery import (
    CATEGORY_CICD_SHARED_NAMESPACE,
    CATEGORY_CLUSTER_CONFIG,
    CATEGORY_MANIFEST_AT_REST,
    CATEGORY_NARRATIVE_REPORT,
    CATEGORY_SECRET_BLOCKED,
    CATEGORY_SOURCE_PATCH,
    MECHANISM_APP_REPO_PR,
    MECHANISM_CLUSTER_ADMIN_REVIEW_GATE,
    MECHANISM_DIRECT_APPLY,
    MECHANISM_INFRA_REPO_COMMIT,
    MECHANISM_SOURCE_REPO_PR,
    classify_file,
    is_gitops_registered,
    route_and_deliver,
)
from conftest import make_async_store, make_report


def _cluster_config_file() -> dict:
    return {
        "category": "skills",
        "path": "app-network-policy.yaml",
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


def _source_patch_file() -> dict:
    return {
        "category": "codechange",
        "path": "patch-01-Dockerfile",
        "content": "FROM ubi9\n",
        "description": "Dockerfile fix",
        "target_path": "Dockerfile",
    }


def _narrative_report_file() -> dict:
    return {
        "category": "dependency",
        "path": "dependency-report.md",
        "content": "# Dependency report\n",
        "description": "dependency report",
    }


def _manifest_at_rest_file() -> dict:
    return {
        "category": "dependency",
        "path": "renovate.json",
        "content": '{"extends": ["config:base"]}',
        "description": "Renovate config",
    }


def _secret_file() -> dict:
    return {
        "category": "skills",
        "path": "db-secret.yaml",
        "content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: c2VjcmV0\n",
        "description": "should never be delivered",
    }


class TestClassifyFile:
    """One test per taxonomy category (docs/unified-apply-flow.md section (D))."""

    def test_cluster_app_config(self):
        assert classify_file(_cluster_config_file()) == CATEGORY_CLUSTER_CONFIG

    def test_cicd_shared_namespace(self):
        assert classify_file(_cicd_file()) == CATEGORY_CICD_SHARED_NAMESPACE

    def test_source_patch_from_codechange_category(self):
        assert classify_file(_source_patch_file()) == CATEGORY_SOURCE_PATCH

    def test_codechange_summary_is_narrative_not_source_patch(self):
        summary = {"category": "codechange", "path": "code-changes-summary.md", "content": "# summary"}
        assert classify_file(summary) == CATEGORY_NARRATIVE_REPORT

    def test_narrative_report_excluded_from_delivery(self):
        assert classify_file(_narrative_report_file()) == CATEGORY_NARRATIVE_REPORT

    def test_manifest_at_rest_for_non_yaml_config(self):
        assert classify_file(_manifest_at_rest_file()) == CATEGORY_MANIFEST_AT_REST

    def test_secret_kind_is_hard_blocked(self):
        assert classify_file(_secret_file()) == CATEGORY_SECRET_BLOCKED

    def test_unparseable_yaml_falls_back_to_manifest_at_rest(self):
        entry = {"category": "skills", "path": "broken.yaml", "content": ": : :not yaml", "description": ""}
        assert classify_file(entry) == CATEGORY_MANIFEST_AT_REST

    def test_missing_category_key_does_not_crash(self):
        """AutoMode's existing tests pass file dicts with no `category` key
        at all -- classify_file must default gracefully, not KeyError."""
        entry = {"path": "x.yaml", "content": "kind: Pod"}
        # `kind: Pod` alone has no apiVersion/metadata -- not a parseable
        # K8s doc via _parse_manifest's own yaml.safe_load_all, so this is
        # manifest_at_rest, not cluster_config. Either way, no crash.
        assert classify_file(entry) in (CATEGORY_CLUSTER_CONFIG, CATEGORY_MANIFEST_AT_REST)


class TestIsGitopsRegistered:
    async def test_falls_back_to_infra_repo_url_when_kube_unreachable(self):
        """KUBECONFIG is invalid in the test environment -- the kube call
        fails fast, and registration falls back to `report.infra_repo_url`."""
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        registered, url = await is_gitops_registered("test-app", report)
        assert registered is True
        assert url == "https://github.com/org/infra-gitops"

    async def test_not_registered_when_no_report_and_kube_unreachable(self):
        registered, url = await is_gitops_registered("test-app", None)
        assert registered is False
        assert url is None

    async def test_registered_signal_wins_over_infra_repo_url_when_kube_succeeds(self):
        """A successful kube call that finds no Application means NOT
        registered, even if `infra_repo_url` happens to be set -- the
        design doc's plumbing-gap fix explicitly prefers the live signal."""
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value=None):
            registered, url = await is_gitops_registered("test-app", report)
        assert registered is False
        assert url == "https://github.com/org/infra-gitops"

    async def test_registered_true_when_application_exists(self):
        report = make_report()
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {"name": "managed-test-app"}}) as mock_get:
            registered, _url = await is_gitops_registered("test-app", report)
        assert registered is True
        mock_get.assert_called_once_with(
            "argoproj.io", "v1alpha1", "applications", "managed-test-app",
            namespace="openshift-gitops",
        )


class TestRouteAndDeliverClusterConfig:
    async def test_not_registered_routes_to_direct_apply(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        assert result["registered"] is False
        assert result["mechanisms"]["cluster_config"] == MECHANISM_DIRECT_APPLY
        mock_apply.assert_called_once()

    async def test_registered_routes_to_infra_repo_commit(self):
        store, raw = make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit, \
             patch("agentit.portal.github_pr.ensure_applicationset") as mock_ensure, \
             patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_commit.return_value = {"pr_url": "https://github.com/org/infra-gitops/pull/1",
                                          "commit_url": "https://github.com/org/infra-gitops/commit/abc123", "files_committed": 1}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        assert result["registered"] is True
        assert result["mechanisms"]["cluster_config"] == MECHANISM_INFRA_REPO_COMMIT
        mock_commit.assert_called_once()
        mock_ensure.assert_called_once()
        mock_apply.assert_not_called()

    async def test_dry_run_skips_infra_commit_call(self):
        store, raw = make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=True, force_dry_run_first=False,
            )
        mock_commit.assert_not_called()
        assert result["outcomes"]["cluster_config"]["dry_run"] is True


class TestRouteAndDeliverCicdLane:
    async def test_cicd_files_create_admin_review_gate_never_silent_skip(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        result = await route_and_deliver(
            [_cicd_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False, force_dry_run_first=False,
        )
        assert result["mechanisms"][CATEGORY_CICD_SHARED_NAMESPACE] == MECHANISM_CLUSTER_ADMIN_REVIEW_GATE
        gates = raw.list_gates(status="pending")
        assert len(gates) == 1
        assert gates[0]["gate_type"] == "cluster-admin-review"
        assert "openshift-pipelines" in gates[0]["summary"]
        outcome = result["outcomes"][CATEGORY_CICD_SHARED_NAMESPACE]
        assert outcome["gate_id"] == gates[0]["id"]


class TestRouteAndDeliverSourcePatch:
    async def test_source_patch_routes_to_source_repo_pr_with_target_path(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        with patch("agentit.portal.github_pr.create_source_patch_pr") as mock_pr:
            mock_pr.return_value = {"pr_url": "https://github.com/org/test-app/pull/9", "branch": "agentit/codechange", "files_committed": 1}
            result = await route_and_deliver(
                [_source_patch_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        assert result["mechanisms"][CATEGORY_SOURCE_PATCH] == MECHANISM_SOURCE_REPO_PR
        mock_pr.assert_called_once()
        called_files = mock_pr.call_args[0][2]
        assert called_files[0]["target_path"] == "Dockerfile"


class TestRouteAndDeliverManifestAtRest:
    async def test_non_yaml_config_routes_to_app_repo_pr(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        with patch("agentit.portal.github_pr.create_onboarding_pr") as mock_pr:
            mock_pr.return_value = {"pr_url": "https://github.com/org/test-app/pull/3", "branch": "agentit/onboarding", "files_added": 1}
            result = await route_and_deliver(
                [_manifest_at_rest_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        assert result["mechanisms"][CATEGORY_MANIFEST_AT_REST] == MECHANISM_APP_REPO_PR
        mock_pr.assert_called_once()


class TestRouteAndDeliverSecretsAndNarrative:
    async def test_secret_never_delivered(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        result = await route_and_deliver(
            [_secret_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False, force_dry_run_first=False,
        )
        assert result["blocked"] == ["db-secret.yaml"]
        assert result["mechanisms"] == {}
        assert result["outcomes"] == {}

    async def test_narrative_report_excluded_not_delivered(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        result = await route_and_deliver(
            [_narrative_report_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False, force_dry_run_first=False,
        )
        assert result["excluded"] == ["dependency-report.md"]
        assert result["mechanisms"] == {}


class TestDeliveriesTracking:
    async def test_delivery_row_created_with_categories_and_mechanism(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        delivery = raw.get_delivery(result["delivery_id"])
        assert delivery is not None
        assert delivery["assessment_id"] == aid
        assert delivery["app_name"] == report.repo_name
        assert delivery["categories"] == {"cluster_config": 1}
        assert "cluster_config:direct-apply" in delivery["mechanism"]
        assert delivery["status"] == "delivered"
        assert delivery["verification"] == "unknown"

    async def test_list_deliveries_returns_rows_for_assessment(self):
        store, raw = make_async_store()
        report = make_report()
        aid = raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": []}
            await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        deliveries = raw.list_deliveries(aid)
        assert len(deliveries) == 1

    def test_update_delivery_merges_details(self):
        store = make_async_store()[1]
        report = make_report()
        aid = store.save(report)
        delivery_id = store.create_delivery(aid, "app", {"cluster_config": 1}, "direct-apply", details={"a": 1})
        ok = store.update_delivery(delivery_id, status="verified", verification="verified", details={"b": 2})
        assert ok is True
        d = store.get_delivery(delivery_id)
        assert d["status"] == "verified"
        assert d["verification"] == "verified"
        assert d["details"] == {"a": 1, "b": 2}

    def test_update_delivery_returns_false_for_unknown_id(self):
        store = make_async_store()[1]
        assert store.update_delivery("nonexistent", status="verified") is False

    def test_list_pending_gitops_deliveries_filters_by_mechanism_and_verification(self):
        store = make_async_store()[1]
        report = make_report()
        aid = store.save(report)
        gitops_id = store.create_delivery(aid, "app", {}, "infra-repo-commit")
        store.create_delivery(aid, "app", {}, "direct-apply")
        pending = store.list_pending_gitops_deliveries()
        assert [d["id"] for d in pending] == [gitops_id]
