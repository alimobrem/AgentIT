"""Tests for the unified apply flow's router (portal/delivery.py) --
classification into docs/unified-apply-flow.md's taxonomy, GitOps
registration detection, and end-to-end routing through
``route_and_deliver()`` for each category.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit import kube
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
    confirmation_text,
    has_unresolved_placeholders,
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


def _placeholder_cronjob_file() -> dict:
    return {
        "category": "cost",
        "path": "cost-cronjob.yaml",
        "content": (
            "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: cost\n"
            "spec:\n  jobTemplate:\n    spec:\n      template:\n        spec:\n"
            "          containers:\n          - name: job\n"
            "            image: REPLACE_WITH_AGENTIT_IMAGE\n"
        ),
        "description": "unresolved image placeholder",
    }


class TestConfirmationText:
    """Regression coverage for naming the *target cluster* (not just the
    *action*) in the direct-apply confirmation -- see the incident this
    fixes in ``kube.get_current_cluster_identity()``'s docstring."""

    def test_direct_apply_names_the_target_cluster(self):
        with patch(
            "agentit.portal.delivery.kube.get_current_cluster_identity",
            return_value={"label": "https://api.example.com:6443 (context: my-cluster)",
                          "host": "https://api.example.com:6443", "context": "my-cluster", "in_cluster": False},
        ):
            text = confirmation_text(MECHANISM_DIRECT_APPLY)

        assert "https://api.example.com:6443 (context: my-cluster)" in text
        assert "apply these manifests directly to the cluster" in text

    def test_direct_apply_names_unreachable_cluster_when_unresolved(self):
        with patch(
            "agentit.portal.delivery.kube.get_current_cluster_identity",
            return_value={"label": "unknown/unreachable cluster", "host": None, "context": None, "in_cluster": False},
        ):
            text = confirmation_text(MECHANISM_DIRECT_APPLY)

        assert "unknown/unreachable cluster" in text

    def test_infra_repo_commit_does_not_call_cluster_identity(self):
        """The GitOps-commit path never touches a cluster client at all --
        confirming this never regresses into an unnecessary kube call."""
        with patch("agentit.portal.delivery.kube.get_current_cluster_identity") as mock_identity:
            confirmation_text(MECHANISM_INFRA_REPO_COMMIT, infra_repo_url="https://github.com/org/infra-gitops")
        mock_identity.assert_not_called()


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
        """Explicitly force the kube call to fail with the same exception
        type a genuinely unreachable cluster raises (`kube.KubeError` --
        `kube.get_custom_resource()` catches the real connection error from
        the `kubernetes`/`urllib3` client and wraps it in this type before
        it ever reaches `is_gitops_registered()`), so this test is
        deterministic regardless of ambient `KUBECONFIG`/cluster
        reachability rather than depending on an invalid `KUBECONFIG` in
        the test environment. Registration then falls back to
        `report.infra_repo_url`."""
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        with patch(
            "agentit.portal.delivery.kube.get_custom_resource",
            side_effect=kube.KubeError("Failed to get argoproj.io/v1alpha1 applications/managed-test-app: connection refused"),
        ):
            registered, url = await is_gitops_registered("test-app", report)
        assert registered is True
        assert url == "https://github.com/org/infra-gitops"

    async def test_not_registered_when_no_report_and_kube_unreachable(self):
        with patch(
            "agentit.portal.delivery.kube.get_custom_resource",
            side_effect=kube.KubeError("Failed to get argoproj.io/v1alpha1 applications/managed-test-app: connection refused"),
        ):
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
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
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
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
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
        gate_id = result["outcomes"]["cluster_config"].get("gate_id")
        assert gate_id
        gates = await raw.list_gates(status="pending")
        assert any(g["id"] == gate_id and g["gate_type"] == "gitops-pr-pending" for g in gates)

    async def test_placeholder_files_are_not_committed(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
        with patch("agentit.portal.delivery.kube.get_custom_resource", return_value={"metadata": {}}), \
             patch("agentit.portal.github_pr.commit_to_infra_repo") as mock_commit:
            result = await route_and_deliver(
                [_placeholder_cronjob_file(), _cluster_config_file()],
                app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        assert "cost-cronjob.yaml" in result["placeholder_blocked"]
        assert has_unresolved_placeholders(_placeholder_cronjob_file()["content"])
        # Only the non-placeholder cluster_config file is committed.
        committed = mock_commit.call_args[0][2]
        assert [f["path"] for f in committed] == ["app-network-policy.yaml"]

    async def test_dry_run_skips_infra_commit_call(self):
        store, raw = await make_async_store()
        report = make_report()
        report.infra_repo_url = "https://github.com/org/infra-gitops"
        aid = await raw.save(report)
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
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_cicd_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False, force_dry_run_first=False,
        )
        assert result["mechanisms"][CATEGORY_CICD_SHARED_NAMESPACE] == MECHANISM_CLUSTER_ADMIN_REVIEW_GATE
        gates = await raw.list_gates(status="pending")
        assert len(gates) == 1
        assert gates[0]["gate_type"] == "cluster-admin-review"
        assert "openshift-pipelines" in gates[0]["summary"]
        outcome = result["outcomes"][CATEGORY_CICD_SHARED_NAMESPACE]
        assert outcome["gate_id"] == gates[0]["id"]


class TestRouteAndDeliverSourcePatch:
    async def test_source_patch_routes_to_source_repo_pr_with_target_path(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
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
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
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
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_secret_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False, force_dry_run_first=False,
        )
        assert result["blocked"] == ["db-secret.yaml"]
        assert result["mechanisms"] == {}
        assert result["outcomes"] == {}

    async def test_narrative_report_excluded_not_delivered(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        result = await route_and_deliver(
            [_narrative_report_file()], app_name=report.repo_name, namespace="ns",
            report=report, store=store, assessment_id=aid,
            actor="tester", dry_run=False, force_dry_run_first=False,
        )
        assert result["excluded"] == ["dependency-report.md"]
        assert result["mechanisms"] == {}


class TestDeliveriesTracking:
    async def test_delivery_row_created_with_categories_and_mechanism(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery is not None
        assert delivery["assessment_id"] == aid
        assert delivery["app_name"] == report.repo_name
        assert delivery["categories"] == {"cluster_config": 1}
        assert "cluster_config:direct-apply" in delivery["mechanism"]
        assert delivery["status"] == "delivered"
        assert delivery["verification"] == "unknown"

    async def test_list_deliveries_returns_rows_for_assessment(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": [], "skipped": [], "errors": []}
            await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        deliveries = await raw.list_deliveries(aid)
        assert len(deliveries) == 1

    async def test_update_delivery_merges_details(self):
        store = (await make_async_store())[1]
        report = make_report()
        aid = await store.save(report)
        delivery_id = await store.create_delivery(aid, "app", {"cluster_config": 1}, "direct-apply", details={"a": 1})
        ok = await store.update_delivery(delivery_id, status="verified", verification="verified", details={"b": 2})
        assert ok is True
        d = await store.get_delivery(delivery_id)
        assert d["status"] == "verified"
        assert d["verification"] == "verified"
        assert d["details"] == {"a": 1, "b": 2}

    async def test_update_delivery_returns_false_for_unknown_id(self):
        store = (await make_async_store())[1]
        assert await store.update_delivery("nonexistent", status="verified") is False

    async def test_list_pending_gitops_deliveries_filters_by_mechanism_and_verification(self):
        store = (await make_async_store())[1]
        report = make_report()
        aid = await store.save(report)
        gitops_id = await store.create_delivery(aid, "app", {}, "infra-repo-commit")
        await store.create_delivery(aid, "app", {}, "direct-apply")
        pending = await store.list_pending_gitops_deliveries()
        assert [d["id"] for d in pending] == [gitops_id]

    async def test_delivery_records_edited_files_for_traceability(self):
        """The edit-before-apply flow's delivered-content traceability
        requirement: a file carrying the `edited` flag
        (`await store.update_onboarding_file()` sets this) must show up in the
        delivery row's `details.edited_files`, a permanent, queryable fact
        about what was actually delivered vs. what was originally
        generated -- not just a transient UI diff."""
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        edited_file = dict(_cluster_config_file())
        edited_file["original_content"] = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: test\n  original: true\n"
        edited_file["edited"] = True
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            result = await route_and_deliver(
                [edited_file], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery["details"]["edited_files"] == ["app-network-policy.yaml"]

    async def test_delivery_edited_files_empty_when_nothing_edited(self):
        store, raw = await make_async_store()
        report = make_report()
        aid = await raw.save(report)
        with patch("agentit.portal.cluster_apply.apply_manifests_to_cluster") as mock_apply:
            mock_apply.return_value = {"applied": ["app-network-policy.yaml"], "skipped": [], "errors": []}
            result = await route_and_deliver(
                [_cluster_config_file()], app_name=report.repo_name, namespace="ns",
                report=report, store=store, assessment_id=aid,
                actor="tester", dry_run=False, force_dry_run_first=False,
            )
        delivery = await raw.get_delivery(result["delivery_id"])
        assert delivery["details"]["edited_files"] == []
