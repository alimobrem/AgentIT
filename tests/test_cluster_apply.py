from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agentit.portal.cluster_apply import apply_manifests_to_cluster


@pytest.fixture(autouse=True)
def _mock_cli():
    """Make _find_cli always return 'oc'."""
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"):
        yield


def _file(path: str, content: str = "") -> dict:
    return {
        "category": "test",
        "path": path,
        "content": content or f"# content of {path}",
        "description": f"desc for {path}",
    }


# ------------------------------------------------------------------


def test_apply_manifests_skips_non_yaml():
    files = [
        _file("setup.sh"),
        _file("README.md"),
        _file("config.json"),
        _file("notes.txt"),
    ]
    with patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert sorted(result["skipped"]) == sorted(
        ["setup.sh", "README.md", "config.json", "notes.txt"]
    )
    assert result["errors"] == []
    mock_run.assert_not_called()


def test_apply_manifests_applies_yaml():
    files = [
        _file("network-policy.yaml", "apiVersion: v1\nkind: NetworkPolicy"),
        _file("service-monitor.yml", "apiVersion: monitoring.coreos.com/v1"),
    ]
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="created", stderr="",
    )
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run:
        result = apply_manifests_to_cluster(files, namespace="myns")

    assert result["applied"] == ["network-policy.yaml", "service-monitor.yml"]
    assert result["skipped"] == []
    assert result["errors"] == []
    assert mock_run.call_count == 2

    for call_args in mock_run.call_args_list:
        cmd = call_args[0][0]
        assert cmd[0] == "oc"
        assert "-n" in cmd
        idx = cmd.index("-n")
        assert cmd[idx + 1] == "myns"
        assert "--dry-run=client" not in cmd


def test_apply_manifests_dry_run():
    files = [
        _file("deployment.yaml", "apiVersion: apps/v1\nkind: Deployment"),
    ]
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="created (dry run)", stderr="",
    )
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run:
        result = apply_manifests_to_cluster(files, dry_run=True)

    assert result["applied"] == ["deployment.yaml"]
    cmd = mock_run.call_args[0][0]
    assert "--dry-run=client" in cmd


def test_apply_manifests_records_errors():
    files = [_file("bad.yaml", "invalid")]
    failed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="error: invalid yaml",
    )
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=failed):
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == []
    assert len(result["errors"]) == 1
    assert "bad.yaml" in result["errors"][0]


def test_apply_manifests_mixed():
    files = [
        _file("deploy.yaml", "apiVersion: apps/v1"),
        _file("install.sh"),
        _file("monitor.yml", "apiVersion: v1"),
    ]
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ok", stderr="",
    )
    with patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed):
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == ["deploy.yaml", "monitor.yml"]
    assert result["skipped"] == ["install.sh"]
    assert result["errors"] == []


def test_find_cli_falls_back_to_kubectl():
    """When oc is missing, kubectl is used."""
    def which_side_effect(cmd):
        return "/usr/bin/kubectl" if cmd == "kubectl" else None

    files = [_file("svc.yaml", "apiVersion: v1")]
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ok", stderr="",
    )
    with (
        patch("agentit.portal.cluster_apply.shutil.which", side_effect=which_side_effect),
        patch("agentit.portal.cluster_apply.subprocess.run", return_value=completed) as mock_run,
    ):
        result = apply_manifests_to_cluster(files)

    assert result["applied"] == ["svc.yaml"]
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "kubectl"


def test_find_cli_raises_when_none():
    files = [_file("x.yaml", "apiVersion: v1")]
    with patch("agentit.portal.cluster_apply.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="Neither oc nor kubectl"):
            apply_manifests_to_cluster(files)
