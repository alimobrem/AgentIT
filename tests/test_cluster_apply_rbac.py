"""Tests for cluster apply: RBAC, missing CLI, YAML vs non-YAML filtering."""
from __future__ import annotations

from unittest.mock import patch

from agentit.portal.cluster_apply import apply_manifests_to_cluster


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
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        result = apply_manifests_to_cluster(files, dry_run=True)

    assert len(result["repo_files"]) == 4
    assert len(result["applied"]) == 0
    mock_kube.apply_yaml.assert_not_called()


def test_applies_yaml_files():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        result = apply_manifests_to_cluster([_yaml_file()], namespace="test-ns", dry_run=False)

    assert result["applied"] == ["test.yaml"]
    mock_kube.apply_yaml.assert_called_once()
    call_args = mock_kube.apply_yaml.call_args
    assert call_args[0][1] == "test-ns"


def test_dry_run_calls_apply_yaml_with_dry_run_flag():
    """Dry run must be a real server-side-apply dry run against the API
    server, not a no-op that only checks for a recognizable `kind`."""
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        result = apply_manifests_to_cluster([_yaml_file()], dry_run=True)

    assert result["applied"] == ["test.yaml"]
    mock_kube.apply_yaml.assert_called_once()
    assert mock_kube.apply_yaml.call_args.kwargs["dry_run"] is True


def test_skips_non_k8s_yml():
    f = {
        "category": "dep",
        "path": "dependabot.yml",
        "content": "version: 2\nupdates:\n  - package-ecosystem: npm",
        "description": "dependabot",
    }
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        result = apply_manifests_to_cluster([f], dry_run=True)

    assert result["applied"] == []
    assert len(result["skipped"]) == 1
    mock_kube.apply_yaml.assert_not_called()


def test_captures_errors():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": False, "error": "forbidden: User cannot create"}
        result = apply_manifests_to_cluster([_yaml_file()], dry_run=False)

    assert len(result["errors"]) == 1
    assert "forbidden" in result["errors"][0]


def test_dry_run_failure_is_a_real_error_not_false_ok():
    """A dry run that hits a real apiserver rejection must surface it as a
    genuine error, never silently report success."""
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": False, "error": "forbidden: User cannot create"}
        result = apply_manifests_to_cluster([_yaml_file()], dry_run=True)

    mock_kube.apply_yaml.assert_called_once()
    assert result["applied"] == []
    assert len(result["errors"]) == 1
    assert "forbidden" in result["errors"][0]


def test_apply_called_when_not_dry_run():
    with patch("agentit.portal.cluster_apply.kube") as mock_kube:
        mock_kube.namespace_exists.return_value = True
        mock_kube.get_api_resources.return_value = set()
        mock_kube.apply_yaml.return_value = {"applied": True, "error": None}
        apply_manifests_to_cluster([_yaml_file()], dry_run=False)

    mock_kube.apply_yaml.assert_called_once()
