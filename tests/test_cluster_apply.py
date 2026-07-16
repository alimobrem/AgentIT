from __future__ import annotations

from unittest.mock import patch

import pytest

from agentit.portal.cluster_apply import apply_manifests_to_cluster


@pytest.fixture(autouse=True)
def _mock_kube():
    """Mock kube module calls: skip namespace checks and api-resources."""
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        yield mock_kube


def _file(path: str, content: str = "") -> dict:
    return {
        "category": "test",
        "path": path,
        "content": content or f"# content of {path}",
        "description": f"desc for {path}",
    }


def _k8s_yaml(kind: str = "ConfigMap", name: str = "test", ns: str = "") -> str:
    doc = f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: {name}"
    if ns:
        doc += f"\n  namespace: {ns}"
    return doc


def test_skips_non_yaml(_mock_kube):
    files = [_file("setup.sh"), _file("README.md"), _file("config.json")]
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["repo_files"]) == 3
    assert result["errors"] == []
    _mock_kube.apply_yaml.assert_not_called()


def test_skips_non_k8s_yaml(_mock_kube):
    files = [_file("dependabot.yml", "version: 2\nupdates:\n  - package-ecosystem: npm")]
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "not a K8s manifest" in result["skipped"][0] or "missing kind" in result["skipped"][0]
    _mock_kube.apply_yaml.assert_not_called()


def test_applies_valid_k8s_yaml(_mock_kube):
    files = [
        _file("configmap.yaml", _k8s_yaml("ConfigMap", "app-config")),
        _file("service.yml", _k8s_yaml("Service", "app-svc")),
    ]
    result = apply_manifests_to_cluster(files, namespace="myns")

    assert sorted(result["applied"]) == ["configmap.yaml", "service.yml"]
    assert result["errors"] == []
    assert _mock_kube.apply_yaml.call_count == 2
    for call_args in _mock_kube.apply_yaml.call_args_list:
        assert call_args[0][1] == "myns"  # namespace arg


def test_dry_run_flag(_mock_kube):
    """Dry run must be a REAL server-side-apply dry run, not just a
    filename-recording no-op (finding: a dry run reported "32/32 OK" while
    zero cluster was actually reachable) -- it calls kube.apply_yaml() with
    dry_run=True instead of skipping the call entirely."""
    files = [_file("deploy.yaml", _k8s_yaml("Deployment", "app"))]
    result = apply_manifests_to_cluster(files, dry_run=True)

    assert result["applied"] == ["deploy.yaml"]
    _mock_kube.apply_yaml.assert_called_once()
    assert _mock_kube.apply_yaml.call_args.kwargs["dry_run"] is True


def test_fixes_namespace_mismatch(_mock_kube):
    files = [_file("svc.yaml", _k8s_yaml("Service", "app", ns="default"))]
    result = apply_manifests_to_cluster(files, namespace="production")

    assert result["applied"] == ["svc.yaml"]


def test_skips_cluster_scoped(_mock_kube):
    content = "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: admin"
    files = [_file("clusterrole.yaml", content)]
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "cluster-scoped" in result["skipped"][0]
    _mock_kube.apply_yaml.assert_not_called()


def test_skips_operator_namespace(_mock_kube):
    content = _k8s_yaml("Application", "my-app", ns="openshift-gitops")
    files = [_file("argoapp.yaml", content)]
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "operator namespace" in result["skipped"][0]
    _mock_kube.apply_yaml.assert_not_called()


def test_skips_missing_crd(_mock_kube):
    content = "apiVersion: autoscaling.k8s.io/v1\nkind: VerticalPodAutoscaler\nmetadata:\n  name: vpa"
    files = [_file("vpa.yaml", content)]
    _mock_kube.get_api_resources.return_value = {"deployments", "services", "configmaps", "pods"}
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "CRD not installed" in result["skipped"][0]
    _mock_kube.apply_yaml.assert_not_called()


def test_records_errors(_mock_kube):
    files = [_file("bad.yaml", _k8s_yaml("ConfigMap", "bad"))]
    _mock_kube.apply_yaml.return_value = {"applied": False, "error": "error: invalid"}
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["errors"]) == 1
    assert "bad.yaml" in result["errors"][0]


def test_mixed_files(_mock_kube):
    files = [
        _file("deploy.yaml", _k8s_yaml("Deployment", "app")),
        _file("install.sh"),
        _file("monitor.yml", _k8s_yaml("ServiceMonitor", "mon")),
    ]
    result = apply_manifests_to_cluster(files)

    assert sorted(result["applied"]) == ["deploy.yaml", "monitor.yml"]
    assert len(result["repo_files"]) == 1


def test_fixes_generate_name(_mock_kube):
    content = "apiVersion: tekton.dev/v1\nkind: PipelineRun\nmetadata:\n  generateName: ci-run-"
    files = [_file("run.yaml", content)]
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == ["run.yaml"]


def test_conflict_kept_separate_from_errors(_mock_kube):
    """A field-manager conflict must not be lumped into `errors` -- callers
    need to tell "another manager owns this" apart from an ordinary
    apply failure to react appropriately (route to a human-reviewed gate
    rather than a generic failure)."""
    files = [_file("cm.yaml", _k8s_yaml("ConfigMap", "test"))]
    _mock_kube.apply_yaml.return_value = {
        "applied": False, "error": "ConfigMap/test: field-manager conflict -- ...",
        "conflict": True, "conflict_details": [{"kind": "ConfigMap", "name": "test", "namespace": "default", "message": "..."}],
    }
    result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert result["errors"] == []
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["path"] == "cm.yaml"


def test_force_defaults_false_and_is_passed_through(_mock_kube):
    files = [_file("cm.yaml", _k8s_yaml("ConfigMap", "test"))]
    apply_manifests_to_cluster(files)
    assert _mock_kube.apply_yaml.call_args.kwargs["force"] is False


def test_force_true_is_threaded_to_kube_apply_yaml(_mock_kube):
    files = [_file("cm.yaml", _k8s_yaml("ConfigMap", "test"))]
    apply_manifests_to_cluster(files, force=True)
    assert _mock_kube.apply_yaml.call_args.kwargs["force"] is True


def test_dry_run_reports_conflicts_from_the_real_dry_run_call(_mock_kube):
    """Dry-run now calls kube.apply_yaml() for real (dry_run=True) -- a
    field-manager conflict surfaced by that call must still be routed to
    `conflicts`, not silently dropped or lumped into `errors`."""
    files = [_file("cm.yaml", _k8s_yaml("ConfigMap", "test"))]
    _mock_kube.apply_yaml.return_value = {
        "applied": False, "error": "ConfigMap/test: field-manager conflict -- ...",
        "conflict": True, "conflict_details": [{"kind": "ConfigMap", "name": "test", "namespace": "default", "message": "..."}],
    }
    result = apply_manifests_to_cluster(files, dry_run=True)
    assert result["applied"] == []
    assert result["errors"] == []
    assert len(result["conflicts"]) == 1
    _mock_kube.apply_yaml.assert_called_once()
    assert _mock_kube.apply_yaml.call_args.kwargs["dry_run"] is True


def test_dry_run_happy_path_still_reports_all_ok(_mock_kube):
    """The pre-existing "N/N OK" happy path (mocked API accepts everything)
    must still work under the new real-dry-run call."""
    files = [_file("cm.yaml", _k8s_yaml("ConfigMap", "test")), _file("svc.yaml", _k8s_yaml("Service", "svc"))]
    result = apply_manifests_to_cluster(files, dry_run=True)
    assert sorted(result["applied"]) == ["cm.yaml", "svc.yaml"]
    assert result["errors"] == []
    assert result["conflicts"] == []


def test_dry_run_surfaces_a_real_failure_not_false_ok(_mock_kube):
    """A dry run that hits a real apiserver rejection (missing CRD, RBAC
    denial, admission-webhook rejection, unreachable cluster, ...) must
    report it as a genuine per-manifest error, never a false "OK" --
    exactly the failure mode the live incident exposed."""
    files = [_file("cm.yaml", _k8s_yaml("ConfigMap", "test"))]
    _mock_kube.apply_yaml.return_value = {
        "applied": False, "error": "ConfigMap (v1) not found on cluster: connection refused",
        "conflict": False, "conflict_details": [],
    }
    result = apply_manifests_to_cluster(files, dry_run=True)
    assert result["applied"] == []
    assert len(result["errors"]) == 1
    assert "connection refused" in result["errors"][0]
