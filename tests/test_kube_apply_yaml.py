"""Tests for `kube.apply_yaml()`'s real per-field-manager server-side-apply
(replacing the old `oc apply --server-side --force-conflicts` subprocess) --
in particular the conflict-vs-other-failure distinction and the `force`/
`field_manager` parameters.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

from agentit import kube

_CM_YAML = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n  namespace: default\ndata:\n  a: b\n"


def _make_conflict_exc(message: str = 'Apply failed with 1 conflict: conflict with "kubectl" using v1: .data.a') -> ApiException:
    exc = ApiException(status=409, reason="Conflict")
    exc.body = f'{{"message": "{message}"}}'
    return exc


def _mock_dynamic_client(server_side_apply_side_effect=None, resources_get_side_effect=None):
    dyn = MagicMock()
    resource = MagicMock()
    resource.namespaced = True
    if resources_get_side_effect is not None:
        dyn.resources.get.side_effect = resources_get_side_effect
    else:
        dyn.resources.get.return_value = resource
    if server_side_apply_side_effect is not None:
        dyn.server_side_apply.side_effect = server_side_apply_side_effect
    return dyn


class TestApplyYamlSuccess:
    def test_clean_apply_returns_applied_true(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default")

        assert result == {"applied": True, "error": None, "conflict": False, "conflict_details": []}
        dyn.server_side_apply.assert_called_once()
        call_kwargs = dyn.server_side_apply.call_args.kwargs
        assert call_kwargs["field_manager"] == "agentit"
        assert call_kwargs["force_conflicts"] is False

    def test_empty_content_is_a_no_op_success(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml("", "default")
        assert result["applied"] is True
        dyn.server_side_apply.assert_not_called()

    def test_custom_field_manager_is_passed_through(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            kube.apply_yaml(_CM_YAML, "default", field_manager="custom-manager")
        assert dyn.server_side_apply.call_args.kwargs["field_manager"] == "custom-manager"

    def test_cluster_scoped_resource_passes_no_namespace(self):
        dyn = MagicMock()
        resource = MagicMock()
        resource.namespaced = False
        dyn.resources.get.return_value = resource
        content = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: my-ns\n"
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            kube.apply_yaml(content, "default")
        assert dyn.server_side_apply.call_args.kwargs["namespace"] is None


class TestApplyYamlConflictVsOtherFailure:
    """The core distinction this rewrite exists to make: a 409 field-manager
    conflict must never be silently forced through or lumped in with a
    generic failure."""

    def test_409_conflict_is_reported_distinctly_not_forced(self):
        dyn = _mock_dynamic_client(server_side_apply_side_effect=_make_conflict_exc())
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default")

        assert result["applied"] is False
        assert result["conflict"] is True
        assert "conflict" in result["error"].lower()
        assert len(result["conflict_details"]) == 1
        assert result["conflict_details"][0]["kind"] == "ConfigMap"
        assert result["conflict_details"][0]["name"] == "test"
        # Never silently forced: force_conflicts must reflect the caller's
        # explicit (default False) choice, not be flipped to True on 409.
        assert dyn.server_side_apply.call_args.kwargs["force_conflicts"] is False

    def test_non_409_failure_is_a_plain_error_not_a_conflict(self):
        forbidden = ApiException(status=403, reason="Forbidden")
        forbidden.body = '{"message": "User cannot patch resource"}'
        dyn = _mock_dynamic_client(server_side_apply_side_effect=forbidden)
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default")

        assert result["applied"] is False
        assert result["conflict"] is False
        assert result["conflict_details"] == []
        assert "error" not in result["error"].lower() or "forbidden" in result["error"].lower() or result["error"]

    def test_generic_exception_is_a_plain_error_not_a_conflict(self):
        dyn = _mock_dynamic_client(server_side_apply_side_effect=RuntimeError("connection reset"))
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default")

        assert result["applied"] is False
        assert result["conflict"] is False
        assert "connection reset" in result["error"]

    def test_force_true_is_passed_through_as_force_conflicts(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default", force=True)

        assert result["applied"] is True
        assert dyn.server_side_apply.call_args.kwargs["force_conflicts"] is True

    def test_hard_failure_takes_precedence_over_conflict_in_same_multidoc_call(self):
        """One `content` call can span multiple documents -- if one document
        conflicts and another hard-fails, the hard failure wins (`conflict`
        stays False) since it needs attention regardless of ownership."""
        multi_doc = (
            _CM_YAML
            + "---\n"
            + "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: other\n  namespace: default\n"
        )
        dyn = _mock_dynamic_client(
            server_side_apply_side_effect=[_make_conflict_exc(), RuntimeError("boom")],
        )
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(multi_doc, "default")

        assert result["applied"] is False
        assert result["conflict"] is False
        assert result["conflict_details"] == []


class TestApplyYamlDryRun:
    """Regression coverage for Finding #2: dry-run must actually reach the
    K8s API server's own dryRun=All validation, not just check that the
    manifest has a recognizable `kind`."""

    def test_dry_run_true_passes_dry_run_all_to_server_side_apply(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default", dry_run=True)

        assert result["applied"] is True
        dyn.server_side_apply.assert_called_once()
        assert dyn.server_side_apply.call_args.kwargs["dry_run"] == "All"

    def test_dry_run_false_passes_no_dry_run_query_param(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            kube.apply_yaml(_CM_YAML, "default", dry_run=False)

        assert dyn.server_side_apply.call_args.kwargs["dry_run"] is None

    def test_dry_run_surfaces_a_real_admission_rejection_as_an_error(self):
        """A dry-run against a real apiserver can genuinely fail (e.g. an
        admission webhook or CRD schema validation rejecting the manifest)
        -- that must surface as a real per-manifest error, never a false
        "OK"."""
        rejected = ApiException(status=422, reason="Unprocessable Entity")
        rejected.body = '{"message": "admission webhook denied the request: invalid spec"}'
        dyn = _mock_dynamic_client(server_side_apply_side_effect=rejected)
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default", dry_run=True)

        assert result["applied"] is False
        assert "admission webhook denied" in result["error"] or "Unprocessable" in result["error"]

    def test_dry_run_surfaces_missing_crd_as_an_error_not_false_ok(self):
        """A dry-run against a cluster with no reachable API / a missing
        CRD must not silently report success -- this is exactly the
        incident the finding describes: a dry run reporting "32/32 OK"
        while zero cluster was actually reachable."""
        dyn = _mock_dynamic_client(resources_get_side_effect=RuntimeError("no matches for kind"))
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default", dry_run=True)

        assert result["applied"] is False
        assert "not found on cluster" in result["error"]
        dyn.server_side_apply.assert_not_called()

    def test_dry_run_unreachable_cluster_is_a_real_error_not_false_ok(self):
        """No cluster reachable at all (e.g. connection refused) during a
        dry run must fail informatively, never report false success."""
        dyn = _mock_dynamic_client(server_side_apply_side_effect=RuntimeError("connection refused"))
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default", dry_run=True)

        assert result["applied"] is False
        assert "connection refused" in result["error"]

    def test_dry_run_happy_path_still_reports_ok_when_api_accepts_everything(self):
        """The existing happy path (mocked API accepts everything) must
        keep working under dry_run=True exactly like dry_run=False."""
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default", dry_run=True)

        assert result == {"applied": True, "error": None, "conflict": False, "conflict_details": []}


class TestApplyYamlInvalidInput:
    def test_invalid_yaml_returns_error_not_conflict(self):
        dyn = _mock_dynamic_client()
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml("not: valid: yaml: [", "default")
        assert result["applied"] is False
        assert result["conflict"] is False

    def test_missing_name_is_reported_as_error(self):
        dyn = _mock_dynamic_client()
        content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  namespace: default\n"
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(content, "default")
        assert result["applied"] is False
        assert result["conflict"] is False
        dyn.server_side_apply.assert_not_called()

    def test_kind_not_found_on_cluster_is_reported_as_error(self):
        dyn = _mock_dynamic_client(resources_get_side_effect=RuntimeError("no matches for kind"))
        with patch("agentit.kube.dynamic_client", return_value=dyn):
            result = kube.apply_yaml(_CM_YAML, "default")
        assert result["applied"] is False
        assert result["conflict"] is False
        assert "not found on cluster" in result["error"]
