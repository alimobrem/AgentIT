"""Dry-run validates manifests via kube.apply_yaml(..., dry_run=True) —
Kubernetes server-side-apply dryRun=All — never via kubectl/oc CLI, and
never persists resources on the cluster.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.portal.cluster_apply import dry_run_manifests_against_cluster


def _file(path: str, content: str) -> dict:
    return {"path": path, "content": content, "description": path, "category": "skills"}


def _k8s_yaml(kind: str = "ConfigMap", name: str = "test") -> str:
    return (
        f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: {name}\n"
        f"data:\n  a: b\n"
    )


class TestDryRunManifestsAgainstCluster:
    def test_calls_apply_yaml_with_dry_run_true(self):
        files = [_file("cm.yaml", _k8s_yaml())]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": True, "error": None, "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="app-ns")

        assert result["applied"] == ["cm.yaml"]
        assert result["errors"] == []
        mock_apply.assert_called_once()
        assert mock_apply.call_args.kwargs["dry_run"] is True
        assert mock_apply.call_args.args[1] == "app-ns"

    def test_surfaces_apiserver_rejection_fail_closed(self):
        files = [_file("cm.yaml", _k8s_yaml())]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "ConfigMap/test: admission webhook denied",
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert result["applied"] == []
        assert any("admission webhook" in e for e in result["errors"])

    def test_missing_crd_is_error_not_false_ok(self):
        files = [_file(
            "pipeline.yaml",
            "apiVersion: tekton.dev/v1\nkind: Pipeline\nmetadata:\n"
            "  name: build\n  namespace: openshift-pipelines\n",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "Pipeline (tekton.dev/v1) not found on cluster: no matches for kind",
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert result["applied"] == []
        assert result["errors"]
        assert "Pipeline" in result["missing_operators"]

    def test_operator_namespace_manifests_are_validated_not_skipped(self):
        """CI/CD shared-namespace manifests must SSA-dry-run in their
        declared operator namespace (GitOps will land them there)."""
        files = [_file(
            "pipeline.yaml",
            "apiVersion: tekton.dev/v1\nkind: Pipeline\nmetadata:\n"
            "  name: build\n  namespace: openshift-pipelines\n",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": True, "error": None, "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="app-ns")

        assert result["applied"] == ["pipeline.yaml"]
        body = mock_apply.call_args.args[0]
        assert "openshift-pipelines" in body

    def test_markdown_skipped_as_repo_file(self):
        files = [_file("runbook.md", "# docs\n")]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            result = dry_run_manifests_against_cluster(files, namespace="ns")
        mock_apply.assert_not_called()
        assert result["repo_files"][0]["path"] == "runbook.md"
        assert result["errors"] == []

    def test_unreachable_cluster_exception_fail_closed(self):
        files = [_file("cm.yaml", _k8s_yaml())]
        with patch(
            "agentit.portal.cluster_apply.kube.apply_yaml",
            side_effect=RuntimeError("AGENTIT_OFFLINE is set"),
        ):
            result = dry_run_manifests_against_cluster(files, namespace="ns")
        assert result["applied"] == []
        assert any("AGENTIT_OFFLINE" in e for e in result["errors"])
