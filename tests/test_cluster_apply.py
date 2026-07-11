from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agentit.portal.cluster_apply import apply_manifests_to_cluster


@pytest.fixture(autouse=True)
def _mock_cli():
    """Make _find_cli return 'oc', skip namespace creation and api-resources."""
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()):
        yield


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


def test_skips_non_yaml():
    files = [_file("setup.sh"), _file("README.md"), _file("config.json")]
    with patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["repo_files"]) == 3
    assert result["errors"] == []
    mock_run.assert_not_called()


def test_skips_non_k8s_yaml():
    files = [_file("dependabot.yml", "version: 2\nupdates:\n  - package-ecosystem: npm")]
    with patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "not a K8s manifest" in result["skipped"][0] or "missing kind" in result["skipped"][0]
    mock_run.assert_not_called()


def test_applies_valid_k8s_yaml():
    files = [
        _file("configmap.yaml", _k8s_yaml("ConfigMap", "app-config")),
        _file("service.yml", _k8s_yaml("Service", "app-svc")),
    ]
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="created", stderr="")
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run:
        result = apply_manifests_to_cluster(files, namespace="myns")

    assert sorted(result["applied"]) == ["configmap.yaml", "service.yml"]
    assert result["errors"] == []
    assert mock_run.call_count == 2
    for call_args in mock_run.call_args_list:
        cmd = call_args[0][0]
        assert "-n" in cmd
        assert cmd[cmd.index("-n") + 1] == "myns"


def test_dry_run_flag():
    files = [_file("deploy.yaml", _k8s_yaml("Deployment", "app"))]
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok (dry run)", stderr="")
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run:
        result = apply_manifests_to_cluster(files, dry_run=True)

    assert result["applied"] == ["deploy.yaml"]
    cmd = mock_run.call_args[0][0]
    assert "--dry-run=client" in cmd


def test_fixes_namespace_mismatch():
    files = [_file("svc.yaml", _k8s_yaml("Service", "app", ns="default"))]
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run:
        result = apply_manifests_to_cluster(files, namespace="production")

    assert result["applied"] == ["svc.yaml"]


def test_skips_cluster_scoped():
    content = "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: admin"
    files = [_file("clusterrole.yaml", content)]
    with patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "cluster-scoped" in result["skipped"][0]
    mock_run.assert_not_called()


def test_skips_operator_namespace():
    content = _k8s_yaml("Application", "my-app", ns="openshift-gitops")
    files = [_file("argoapp.yaml", content)]
    with patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "operator namespace" in result["skipped"][0]
    mock_run.assert_not_called()


def test_skips_missing_crd():
    content = "apiVersion: autoscaling.k8s.io/v1\nkind: VerticalPodAutoscaler\nmetadata:\n  name: vpa"
    files = [_file("vpa.yaml", content)]
    available = {"deployments", "services", "configmaps", "pods"}
    with patch("agentit.portal.cluster_apply._get_available_resources", return_value=available), \
         patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    assert "CRD not installed" in result["skipped"][0]
    mock_run.assert_not_called()


def test_records_errors():
    files = [_file("bad.yaml", _k8s_yaml("ConfigMap", "bad"))]
    failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error: invalid")
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=failed):
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["errors"]) == 1
    assert "bad.yaml" in result["errors"][0]


def test_mixed_files():
    files = [
        _file("deploy.yaml", _k8s_yaml("Deployment", "app")),
        _file("install.sh"),
        _file("monitor.yml", _k8s_yaml("ServiceMonitor", "mon")),
    ]
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed):
        result = apply_manifests_to_cluster(files)

    assert sorted(result["applied"]) == ["deploy.yaml", "monitor.yml"]
    assert len(result["repo_files"]) == 1


def test_find_cli_falls_back_to_kubectl():
    def which_side(cmd):
        return "/usr/bin/kubectl" if cmd == "kubectl" else None

    files = [_file("svc.yaml", _k8s_yaml("Service", "app"))]
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with patch("agentit.portal.cluster_apply.shutil.which", side_effect=which_side), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == ["svc.yaml"]
    assert mock_run.call_args[0][0][0] == "kubectl"


def test_find_cli_raises_when_none():
    files = [_file("x.yaml", _k8s_yaml())]
    with patch("agentit.portal.cluster_apply.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="Neither oc nor kubectl"):
            apply_manifests_to_cluster(files)


def test_fixes_generate_name():
    content = "apiVersion: tekton.dev/v1\nkind: PipelineRun\nmetadata:\n  generateName: ci-run-"
    files = [_file("run.yaml", content)]
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed):
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == ["run.yaml"]
