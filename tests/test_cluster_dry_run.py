"""Dry-run validates manifests via kube.apply_yaml(..., dry_run=True) —
Kubernetes server-side-apply dryRun=All — never via kubectl/oc CLI, and
never persists resources on the cluster.

Hard failures (schema/admission/unreachable) block Scan delivery.
Soft failures (Forbidden / missing optional CRD / field-manager conflict)
warn without blocking.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agentit.portal.cluster_apply import (
    classify_dry_run_error,
    dry_run_manifests_against_cluster,
)


def _file(path: str, content: str) -> dict:
    return {"path": path, "content": content, "description": path, "category": "skills"}


def _k8s_yaml(kind: str = "ConfigMap", name: str = "test") -> str:
    return (
        f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: {name}\n"
        f"data:\n  a: b\n"
    )


class TestClassifyDryRunError:
    @pytest.mark.parametrize(
        "message",
        [
            "Role/pinky-compliance-reader: Forbidden",
            "LimitRange/pinky-limits: Forbidden",
            'Error from server (Forbidden): roles.rbac.authorization.k8s.io is forbidden',
            "ConfigMap/test: (403)\nReason: Forbidden",
        ],
    )
    def test_forbidden_is_soft(self, message: str):
        assert classify_dry_run_error(message) == "soft"

    @pytest.mark.parametrize(
        "message",
        [
            "Policy (kyverno.io/v1) not found on cluster: no matches for kind",
            "Pipeline (tekton.dev/v1) not found on cluster: no matches for kind",
            "ChaosEngine (litmuschaos.io/v1alpha1) not found on cluster: the server could not find the requested resource",
            "unable to recognize \"\": no matches for kind \"Policy\" in version \"kyverno.io/v1\"",
        ],
    )
    def test_missing_crd_is_soft(self, message: str):
        assert classify_dry_run_error(message) == "soft"

    @pytest.mark.parametrize(
        "message",
        [
            "Pipeline/pinky-pipeline: Bad Request",
            "ConfigMap/test: Unprocessable Entity",
            "ConfigMap/test: admission webhook denied the request: invalid spec",
            "AGENTIT_OFFLINE is set",
            "Kubernetes circuit breaker open — too many recent API failures",
            "invalid YAML: mapping values are not allowed",
            "connection refused",
        ],
    )
    def test_schema_admission_unreachable_are_hard(self, message: str):
        assert classify_dry_run_error(message) == "hard"

    def test_empty_message_is_hard(self):
        assert classify_dry_run_error("") == "hard"
        assert classify_dry_run_error(None) == "hard"  # type: ignore[arg-type]


class TestDryRunManifestsAgainstCluster:
    def test_calls_apply_yaml_with_dry_run_true(self):
        files = [_file("cm.yaml", _k8s_yaml())]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": True, "error": None, "errors": [],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="app-ns")

        assert result["applied"] == ["cm.yaml"]
        assert result["errors"] == []
        assert result["warnings"] == []
        mock_apply.assert_called_once()
        assert mock_apply.call_args.kwargs["dry_run"] is True
        assert mock_apply.call_args.args[1] == "app-ns"

    def test_surfaces_admission_rejection_as_hard_error(self):
        files = [_file("cm.yaml", _k8s_yaml())]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "ConfigMap/test: admission webhook denied",
                "errors": ["ConfigMap/test: admission webhook denied"],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert result["applied"] == []
        assert any("admission webhook" in e for e in result["errors"])
        assert result["warnings"] == []

    def test_bad_request_when_crd_exists_is_hard(self):
        """Pinky Pipeline Bad Request must stay hard — CRD is present."""
        files = [_file(
            "pinky-tekton-pipeline.yaml",
            "apiVersion: tekton.dev/v1\nkind: Pipeline\nmetadata:\n"
            "  name: pinky-pipeline\n  namespace: openshift-pipelines\n",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "Pipeline/pinky-pipeline: Bad Request",
                "errors": ["Pipeline/pinky-pipeline: Bad Request"],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert any("Bad Request" in e for e in result["errors"])
        assert result["warnings"] == []

    def test_missing_crd_is_soft_warning_not_hard_error(self):
        files = [_file(
            "pipeline.yaml",
            "apiVersion: tekton.dev/v1\nkind: Pipeline\nmetadata:\n"
            "  name: build\n  namespace: openshift-pipelines\n",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "Pipeline (tekton.dev/v1) not found on cluster: no matches for kind",
                "errors": [
                    "Pipeline (tekton.dev/v1) not found on cluster: no matches for kind",
                ],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert result["errors"] == []
        assert result["warnings"]
        assert "Pipeline" in result["missing_operators"]

    def test_forbidden_is_soft_warning_not_hard_error(self):
        files = [_file(
            "role.yaml",
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n"
            "metadata:\n  name: pinky-compliance-reader\n",
        )]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "Role/pinky-compliance-reader: Forbidden",
                "errors": ["Role/pinky-compliance-reader: Forbidden"],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert result["errors"] == []
        assert any("Forbidden" in w for w in result["warnings"])

    def test_field_manager_conflict_is_soft_warning_not_hard_error(self):
        """Re-onboard with prior kubectl CSA ConfigMaps must not block PRs."""
        files = [_file("pinky-cost-labels.yaml", _k8s_yaml("ConfigMap", "pinky-cost-labels"))]
        conflict_msg = (
            'ConfigMap/pinky-cost-labels: field-manager conflict -- '
            'Apply failed with 1 conflict: conflict with '
            '"kubectl-client-side-apply" using v1: .data.cost-center'
        )
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": conflict_msg,
                "errors": [],
                "conflict": True,
                "conflict_details": [{
                    "kind": "ConfigMap",
                    "name": "pinky-cost-labels",
                    "message": conflict_msg,
                }],
            }
            result = dry_run_manifests_against_cluster(files, namespace="pinky")

        assert result["errors"] == []
        assert result["conflicts"]
        assert any("field-manager conflict" in w for w in result["warnings"])

    def test_mixed_soft_and_hard_in_one_file_keeps_hard(self):
        """Per-doc errors: soft Forbidden must not hide hard Bad Request."""
        multi = (
            "apiVersion: v1\nkind: LimitRange\nmetadata:\n  name: limits\n"
            "---\n"
            "apiVersion: tekton.dev/v1\nkind: Pipeline\nmetadata:\n"
            "  name: bad\n  namespace: openshift-pipelines\n"
        )
        files = [_file("mixed.yaml", multi)]
        with patch("agentit.portal.cluster_apply.kube.apply_yaml") as mock_apply:
            mock_apply.return_value = {
                "applied": False,
                "error": "LimitRange/limits: Forbidden",
                "errors": [
                    "LimitRange/limits: Forbidden",
                    "Pipeline/bad: Bad Request",
                ],
                "conflict": False, "conflict_details": [],
            }
            result = dry_run_manifests_against_cluster(files, namespace="ns")

        assert any("Bad Request" in e for e in result["errors"])
        assert any("Forbidden" in w for w in result["warnings"])

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
                "applied": True, "error": None, "errors": [],
                "conflict": False, "conflict_details": [],
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
        assert result["warnings"] == []

    def test_unreachable_cluster_exception_fail_closed(self):
        files = [_file("cm.yaml", _k8s_yaml())]
        with patch(
            "agentit.portal.cluster_apply.kube.apply_yaml",
            side_effect=RuntimeError("AGENTIT_OFFLINE is set"),
        ):
            result = dry_run_manifests_against_cluster(files, namespace="ns")
        assert result["applied"] == []
        assert any("AGENTIT_OFFLINE" in e for e in result["errors"])
        assert result["warnings"] == []
