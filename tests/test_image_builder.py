"""Tests for image_builder.py — Tekton PipelineRun creation and polling.

These exercise build_app_image/wait_for_build entirely through the mockable
kube.py client interface. No subprocess mocking is needed: an earlier version
of this module shelled out to `oc apply`/`oc get pipelinerun`, which meant
these functions were untestable without either mocking subprocess.run at
every call site or risking real `oc apply` calls against a live cluster (see
test_portal.py's _override_store fixture for the incident that caused).
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.image_builder import build_app_image, wait_for_build


@patch("agentit.image_builder.kube")
def test_build_app_image_success(mock_kube):
    result = build_app_image(
        repo_url="https://github.com/org/my-app.git",
        app_name="my-app",
        namespace="agentit",
    )

    assert "error" not in result
    assert result["status"] == "running"
    assert result["image_ref"].endswith("/agentit/my-app:latest")

    mock_kube.custom_objects.return_value.create_namespaced_custom_object.assert_called_once()
    _, kwargs = mock_kube.custom_objects.return_value.create_namespaced_custom_object.call_args
    assert kwargs["group"] == "tekton.dev"
    assert kwargs["version"] == "v1"
    assert kwargs["namespace"] == "agentit"
    assert kwargs["plural"] == "pipelineruns"
    assert kwargs["body"]["kind"] == "PipelineRun"
    assert kwargs["body"]["spec"]["params"][0]["value"] == "https://github.com/org/my-app.git"


@patch("agentit.image_builder.kube")
def test_build_app_image_api_error(mock_kube):
    mock_kube.custom_objects.return_value.create_namespaced_custom_object.side_effect = Exception(
        "403 Forbidden: pipelineruns.tekton.dev is forbidden",
    )

    result = build_app_image(
        repo_url="https://github.com/org/my-app.git",
        app_name="my-app",
    )

    assert "error" in result
    assert "Failed to create PipelineRun" in result["error"]


@patch("agentit.image_builder.kube")
def test_wait_for_build_succeeded(mock_kube):
    mock_kube.get_custom_resource.return_value = {
        "status": {"conditions": [{"reason": "Succeeded"}]},
    }

    with patch("agentit.image_builder.time.sleep"):
        result = wait_for_build("build-my-app-1", namespace="agentit", timeout=30)

    assert result == {"status": "Succeeded"}
    mock_kube.get_custom_resource.assert_called_with(
        "tekton.dev", "v1", "pipelineruns", "build-my-app-1", namespace="agentit",
    )


@patch("agentit.image_builder.kube")
def test_wait_for_build_failed(mock_kube):
    mock_kube.get_custom_resource.return_value = {
        "status": {"conditions": [{"reason": "Failed"}]},
    }

    with patch("agentit.image_builder.time.sleep"):
        result = wait_for_build("build-my-app-1", namespace="agentit", timeout=30)

    assert result == {"status": "Failed", "reason": "Failed"}


@patch("agentit.image_builder.kube")
def test_wait_for_build_timeout(mock_kube):
    mock_kube.get_custom_resource.return_value = {
        "status": {"conditions": [{"reason": "Running"}]},
    }

    with patch("agentit.image_builder.time.sleep"), \
         patch("agentit.image_builder.time.monotonic", side_effect=[0, 1, 700]):
        result = wait_for_build("build-my-app-1", namespace="agentit", timeout=600)

    assert result == {"status": "Timeout"}


@patch("agentit.image_builder.kube")
def test_wait_for_build_survives_api_errors(mock_kube):
    """A transient API error while polling should not crash — just keep polling."""
    mock_kube.get_custom_resource.side_effect = Exception("connection reset")

    with patch("agentit.image_builder.time.sleep"), \
         patch("agentit.image_builder.time.monotonic", side_effect=[0, 1, 700]):
        result = wait_for_build("build-my-app-1", namespace="agentit", timeout=600)

    assert result == {"status": "Timeout"}
