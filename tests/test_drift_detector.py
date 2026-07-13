"""Tests for the drift detector watcher — regression for the AttributeError
crash from referencing DriftResult.has_warnings / DriftResult.deprecated_apis,
neither of which exist on the real dataclass."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agentit.api_drift_detector import DriftResult
from agentit.kube import KubeError
from agentit.platform_context import PlatformContext
from agentit.watchers.drift_detector import DriftDetector


def _detector() -> DriftDetector:
    return DriftDetector(publisher=MagicMock(), interval=1)


_SYNCED_ARGO_APP = {
    "metadata": {"name": "some-app"},
    "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
}


class TestApiDriftWarnings:
    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    def test_detect_once_does_not_crash_with_deprecated_apis(self, mock_list):
        """Regression: previously raised AttributeError every tick because
        DriftResult has no has_warnings/deprecated_apis attributes. Requires
        Argo CD access to be reachable so the code actually gets to the API
        drift detection block (otherwise detect_once returns early)."""
        mock_list.return_value = [_SYNCED_ARGO_APP]
        detector = _detector()

        ctx = PlatformContext(
            k8s_version="1.25",
            available_kinds={"deployments"},
            deprecated_apis=[{"api": "policy/v1beta1 PodSecurityPolicy", "removed_in": "1.25"}],
        )
        with patch("agentit.platform_context.discover_platform", return_value=ctx), \
             patch("agentit.api_drift_detector.detect_drift", return_value=DriftResult()):
            # Must not raise.
            result = detector.detect_once()

        assert result == []

    @patch("agentit.watchers.drift_detector.kube.list_custom_resources")
    def test_detect_once_reports_deprecated_apis_from_platform_context(self, mock_list, capsys):
        """ctx.deprecated_apis (the real field) should drive the WARNING
        message, not the nonexistent api_drift.deprecated_apis."""
        mock_list.return_value = [_SYNCED_ARGO_APP]
        detector = _detector()

        ctx = PlatformContext(
            k8s_version="1.25",
            available_kinds={"deployments"},
            deprecated_apis=[
                {"api": "policy/v1beta1 PodSecurityPolicy", "removed_in": "1.25"},
                {"api": "autoscaling/v2beta1 HorizontalPodAutoscaler", "removed_in": "1.26"},
            ],
        )
        with patch("agentit.platform_context.discover_platform", return_value=ctx), \
             patch("agentit.api_drift_detector.detect_drift", return_value=DriftResult()):
            detector.detect_once()

        captured = capsys.readouterr()
        assert "2 deprecated API(s)" in captured.err

    def test_drift_result_has_no_has_warnings_field(self):
        """Documents the real DriftResult shape so this doesn't regress silently."""
        result = DriftResult()
        assert not hasattr(result, "has_warnings")
        assert not hasattr(result, "deprecated_apis")
