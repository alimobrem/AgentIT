"""Tests for cluster apply: RBAC, missing CLI, YAML vs non-YAML filtering."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.portal.cluster_apply import apply_manifests_to_cluster

_PATCHES = {
    "agentit.portal.cluster_apply._ensure_namespace": None,
    "agentit.portal.cluster_apply._get_available_resources": set(),
}


def _yaml_file(name="test.yaml"):
    return {
        "category": "security",
        "path": name,
        "content": "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: test\n",
        "description": "test",
    }


def test_skips_non_yaml_files():
    files = [
        {"category": "c", "path": "script.sh", "content": "#!/bin/bash", "description": "d"},
        {"category": "c", "path": "report.md", "content": "# Report", "description": "d"},
        {"category": "c", "path": "dashboard.json", "content": "{}", "description": "d"},
        {"category": "c", "path": "Containerfile", "content": "FROM ubi", "description": "d"},
    ]
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster(files, dry_run=True)

    assert len(result["repo_files"]) == 4
    assert len(result["applied"]) == 0
    mock_run.assert_not_called()


def test_applies_yaml_files():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "serviceaccount/test created"
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run", return_value=mock_result) as mock_run:
        result = apply_manifests_to_cluster([_yaml_file()], namespace="test-ns", dry_run=True)

    assert result["applied"] == ["test.yaml"]
    args = mock_run.call_args[0][0]
    assert "oc" in args
    assert "--dry-run=client" in args
    assert "-n" in args
    assert "test-ns" in args


def test_skips_non_k8s_yml():
    f = {
        "category": "dep",
        "path": "dependabot.yml",
        "content": "version: 2\nupdates:\n  - package-ecosystem: npm",
        "description": "dependabot",
    }
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/kubectl"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run") as mock_run:
        result = apply_manifests_to_cluster([f], dry_run=True)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    mock_run.assert_not_called()


def test_captures_errors():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "forbidden: User cannot create"
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run", return_value=mock_result):
        result = apply_manifests_to_cluster([_yaml_file()], dry_run=True)

    assert len(result["errors"]) == 1
    assert "forbidden" in result["errors"][0]


def test_no_cli_raises():
    import pytest
    with patch("agentit.portal.cluster_apply.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="Neither oc nor kubectl"):
            apply_manifests_to_cluster([_yaml_file()])


def test_dry_run_flag_passed():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "created (dry run)"
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run", return_value=mock_result) as mock_run:
        apply_manifests_to_cluster([_yaml_file()], dry_run=True)

    args = mock_run.call_args[0][0]
    assert "--dry-run=client" in args


def test_no_dry_run_flag_when_false():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "created"
    with patch("agentit.portal.cluster_apply.shutil.which", return_value="/usr/bin/oc"), \
         patch("agentit.portal.cluster_apply._ensure_namespace"), \
         patch("agentit.portal.cluster_apply._get_available_resources", return_value=set()), \
         patch("agentit.portal.cluster_apply.subprocess.run", return_value=mock_result) as mock_run:
        apply_manifests_to_cluster([_yaml_file()], dry_run=False)

    args = mock_run.call_args[0][0]
    assert "--dry-run=client" not in args
