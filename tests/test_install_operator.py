"""Tests for install_operator: OwnNamespace vs AllNamespaces routing and RBAC error surfacing.

Regression coverage for the live "Install VPA" failure: the service account has no
cluster-scoped permission to create a Namespace, so the install call returns
{"applied": False, "error": "... Forbidden: ..."}. Before this fix, that surfaced
as a generic "installation failed" with no indication of the real cause, and every
operator (not just OwnNamespace ones like VPA) attempted to create a brand-new
namespace even when the operator's CSV supports AllNamespaces and could have used
the existing openshift-operators namespace instead.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.portal.cluster_apply import install_operator


def _mock_apply(applied: bool, error: str | None = None):
    return patch(
        "agentit.portal.cluster_apply.kube.apply_yaml",
        return_value={"applied": applied, "error": error},
    )


def test_own_namespace_operator_creates_dedicated_namespace():
    """VPA only supports OwnNamespace -- must get its own Namespace + OperatorGroup."""
    with _mock_apply(True) as mock_apply:
        result = install_operator("vertical-pod-autoscaler", "stable", "redhat-operators")

    assert result["status"] == "installing"
    assert result["namespace"] == "openshift-vertical-pod-autoscaler"
    content = mock_apply.call_args[0][0]
    assert "kind: Namespace" in content
    assert "kind: OperatorGroup" in content
    assert "kind: Subscription" in content
    assert mock_apply.call_args[0][1] == "openshift-vertical-pod-autoscaler"


def test_all_namespaces_operator_reuses_openshift_operators():
    """Pipelines supports AllNamespaces -- should NOT create a new namespace/OperatorGroup."""
    with _mock_apply(True) as mock_apply:
        result = install_operator("openshift-pipelines-operator-rh", "latest", "redhat-operators")

    assert result["status"] == "installing"
    assert result["namespace"] == "openshift-operators"
    content = mock_apply.call_args[0][0]
    assert "kind: Namespace" not in content
    assert "kind: OperatorGroup" not in content
    assert "kind: Subscription" in content
    assert mock_apply.call_args[0][1] == "openshift-operators"


def test_rejects_non_redhat_source():
    result = install_operator("some-community-op", "stable", "community-operators")

    assert result["status"] == "error"
    assert "Only Red Hat operators" in result["error"]


def test_forbidden_namespace_create_gets_actionable_message():
    """Regression: the exact live failure -- SA can't create the dedicated namespace."""
    forbidden = (
        'Error from server (Forbidden): namespaces "openshift-vertical-pod-autoscaler" '
        'is forbidden: User "system:serviceaccount:agentit:agentit" cannot patch '
        'resource "namespaces" in API group ""'
    )
    with _mock_apply(False, forbidden):
        result = install_operator("vertical-pod-autoscaler", "stable", "redhat-operators")

    assert result["status"] == "error"
    assert "rbac.operatorInstall" in result["error"]
    assert "OperatorHub" in result["error"]
    assert forbidden in result["error"]


def test_forbidden_subscription_create_gets_actionable_message():
    forbidden = 'Error from server (Forbidden): subscriptions.operators.coreos.com is forbidden'
    with _mock_apply(False, forbidden):
        result = install_operator("openshift-pipelines-operator-rh", "latest", "redhat-operators")

    assert result["status"] == "error"
    assert "rbac.operatorInstall" in result["error"]
    assert forbidden in result["error"]


def test_non_forbidden_error_passed_through_unmodified():
    with _mock_apply(False, "some other transient error"):
        result = install_operator("vertical-pod-autoscaler", "stable", "redhat-operators")

    assert result["status"] == "error"
    assert result["error"] == "some other transient error"
