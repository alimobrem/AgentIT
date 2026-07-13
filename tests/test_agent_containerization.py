"""Tests for agent containerization — CLI run-agent, orchestrator K8s mode, kube helpers, agent registry."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from agentit.cli import main
from agentit.models import AssessmentReport

# conftest.py defines make_report as a plain function (not a fixture)
from conftest import make_report

RESULT_BEGIN = "--- AGENTIT_RESULT_BEGIN ---"
RESULT_END = "--- AGENTIT_RESULT_END ---"


def _extract_result(output: str) -> str:
    """Extract JSON payload from between result markers."""
    b = output.find(RESULT_BEGIN)
    e = output.find(RESULT_END)
    if b != -1 and e != -1:
        return output[b + len(RESULT_BEGIN):e].strip()
    return output.strip()


class TestRunAgentCLI:
    """Tests for the ``run-agent`` CLI command."""

    def _write_report(self, report: AssessmentReport) -> str:
        """Serialize report to a temp JSON file, return path."""
        fd, path = tempfile.mkstemp(suffix=".json")
        Path(path).write_text(report.model_dump_json(), encoding="utf-8")
        return path

    def test_run_security_agent(self, tmp_path: Path):
        report = make_report()
        path = self._write_report(report)
        try:
            runner = CliRunner()
            result = runner.invoke(main, ["run-agent", "security", "--report", path])
            assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
            payload = _extract_result(result.output)
            files = json.loads(payload)
            assert isinstance(files, list)
            assert len(files) > 0
            assert all("path" in f and "content" in f for f in files)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_run_observability_agent(self):
        report = make_report()
        path = self._write_report(report)
        try:
            runner = CliRunner()
            result = runner.invoke(main, ["run-agent", "observability", "--report", path])
            assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
            payload = _extract_result(result.output)
            files = json.loads(payload)
            assert len(files) > 0
        finally:
            Path(path).unlink(missing_ok=True)

    def test_run_all_agents(self):
        """Every registered agent runs successfully via CLI."""
        from agentit.agents.capabilities import AGENT_CLASSES

        report = make_report()
        path = self._write_report(report)
        try:
            runner = CliRunner()
            for name in AGENT_CLASSES:
                result = runner.invoke(main, ["run-agent", name, "--report", path])
                assert result.exit_code == 0, f"Agent {name} failed (exit {result.exit_code}): {result.output}"
                payload = _extract_result(result.output)
                files = json.loads(payload)
                assert isinstance(files, list), f"Agent {name} output not a list"
        finally:
            Path(path).unlink(missing_ok=True)

    def test_unknown_agent(self):
        report = make_report()
        path = self._write_report(report)
        try:
            runner = CliRunner()
            result = runner.invoke(main, ["run-agent", "nonexistent", "--report", path])
            assert result.exit_code != 0
        finally:
            Path(path).unlink(missing_ok=True)

    def test_report_roundtrip(self):
        """AssessmentReport serialization roundtrip via model_dump_json/model_validate_json."""
        report = make_report()
        json_str = report.model_dump_json()
        restored = AssessmentReport.model_validate_json(json_str)
        assert restored.repo_name == report.repo_name
        assert len(restored.scores) == len(report.scores)
        assert restored.overall_score == report.overall_score


class TestOrchestratorK8sMode:
    """Orchestrator local vs kubernetes agent execution modes."""

    def test_local_mode_default(self):
        """Default mode is local (in-process) — no K8s Jobs created."""
        from agentit.agents.orchestrator import FleetOrchestrator

        report = make_report()
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(report=report, output_dir=Path(tmpdir))
            result = orch.run()
            assert len(result.agent_results) > 0
            assert all(ar.success for ar in result.agent_results)

    @patch.dict("os.environ", {"AGENTIT_AGENT_MODE": "kubernetes"})
    @patch("agentit.agents.orchestrator.AGENT_MODE", "kubernetes")
    @patch("agentit.kube.create_config_map", return_value=True)
    @patch("agentit.kube.create_job", return_value=True)
    @patch("agentit.kube.get_job_status", return_value="succeeded")
    @patch("agentit.kube.get_job_pod_log", return_value="--- AGENTIT_RESULT_BEGIN ---\n" + json.dumps([
        {"path": "test.yaml", "content": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test", "description": "test", "finding_addressed": ""}
    ]) + "\n--- AGENTIT_RESULT_END ---")
    @patch("agentit.kube.delete_job")
    @patch("agentit.kube.delete_config_map")
    def test_kubernetes_mode_creates_jobs(
        self, mock_del_cm, mock_del_job, mock_log, mock_status, mock_job, mock_cm
    ):
        """Kubernetes mode creates Jobs and reads results."""
        from agentit.agents.orchestrator import FleetOrchestrator

        report = make_report()
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(report=report, output_dir=Path(tmpdir))
            result = orch.run()
            assert mock_cm.called, "create_config_map not called"
            assert mock_job.called, "create_job not called"
            assert mock_del_job.called, "delete_job not called (cleanup)"
            assert mock_del_cm.called, "delete_config_map not called (cleanup)"

    @patch.dict("os.environ", {"AGENTIT_AGENT_MODE": "kubernetes"})
    @patch("agentit.agents.orchestrator.AGENT_MODE", "kubernetes")
    @patch("agentit.kube.create_config_map", return_value=False)
    def test_kubernetes_mode_fallback_on_configmap_failure(self, mock_cm):
        """Falls back to local mode if ConfigMap creation fails."""
        from agentit.agents.orchestrator import FleetOrchestrator

        report = make_report()
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(report=report, output_dir=Path(tmpdir))
            result = orch.run()
            # Should still produce results (fell back to local)
            assert len(result.agent_results) > 0


class TestKubeJobHelpers:
    """Unit tests for kube.py Job helper functions."""

    @patch("agentit.kube.core_v1")
    def test_create_config_map(self, mock_core_fn):
        # kubernetes.client types are imported inside create_config_map;
        # provide a fake module so the import doesn't blow up.
        import sys
        import types

        fake_k8s = types.ModuleType("kubernetes")
        fake_client = types.ModuleType("kubernetes.client")
        fake_client.V1ConfigMap = MagicMock()
        fake_client.V1ObjectMeta = MagicMock()
        fake_k8s.client = fake_client
        with patch.dict(sys.modules, {
            "kubernetes": fake_k8s,
            "kubernetes.client": fake_client,
        }):
            from agentit import kube

            mock_api = MagicMock()
            mock_core_fn.return_value = mock_api
            mock_api.create_namespaced_config_map.return_value = None

            result = kube.create_config_map("test-cm", "default", {"key": "value"})
            assert result is True
            mock_api.create_namespaced_config_map.assert_called_once()

    @patch("agentit.kube.batch_v1")
    def test_create_job(self, mock_batch_fn):
        import sys
        import types

        fake_k8s = types.ModuleType("kubernetes")
        fake_client = types.ModuleType("kubernetes.client")
        # Provide all types imported in create_job
        for cls_name in (
            "V1Job", "V1JobSpec", "V1ObjectMeta", "V1PodTemplateSpec",
            "V1PodSpec", "V1Container", "V1ResourceRequirements",
            "V1SecurityContext", "V1Volume", "V1VolumeMount",
            "V1ConfigMapVolumeSource", "V1EnvVar",
        ):
            setattr(fake_client, cls_name, MagicMock())
        fake_k8s.client = fake_client
        with patch.dict(sys.modules, {
            "kubernetes": fake_k8s,
            "kubernetes.client": fake_client,
        }):
            from agentit import kube

            mock_api = MagicMock()
            mock_batch_fn.return_value = mock_api
            mock_api.create_namespaced_job.return_value = None

            result = kube.create_job(
                "test-job", "default", "test-image:latest",
                ["python", "-m", "agentit", "run-agent", "security"],
                config_map_name="test-cm",
            )
            assert result is True
            mock_api.create_namespaced_job.assert_called_once()

    @patch("agentit.kube.batch_v1")
    def test_get_job_status_succeeded(self, mock_batch_fn):
        from agentit import kube

        mock_api = MagicMock()
        mock_batch_fn.return_value = mock_api
        mock_job = MagicMock()
        mock_job.status.succeeded = 1
        mock_job.status.failed = None
        mock_job.status.active = None
        mock_api.read_namespaced_job_status.return_value = mock_job

        assert kube.get_job_status("test", "default") == "succeeded"

    @patch("agentit.kube.batch_v1")
    def test_get_job_status_failed(self, mock_batch_fn):
        from agentit import kube

        mock_api = MagicMock()
        mock_batch_fn.return_value = mock_api
        mock_job = MagicMock()
        mock_job.status.succeeded = None
        mock_job.status.failed = 1
        mock_job.status.active = None
        mock_api.read_namespaced_job_status.return_value = mock_job

        assert kube.get_job_status("test", "default") == "failed"

    @patch("agentit.kube.batch_v1")
    def test_get_job_status_active(self, mock_batch_fn):
        from agentit import kube

        mock_api = MagicMock()
        mock_batch_fn.return_value = mock_api
        mock_job = MagicMock()
        mock_job.status.succeeded = None
        mock_job.status.failed = None
        mock_job.status.active = 1
        mock_api.read_namespaced_job_status.return_value = mock_job

        assert kube.get_job_status("test", "default") == "active"

    @patch("agentit.kube.core_v1")
    def test_get_job_pod_log(self, mock_core_fn):
        from agentit import kube

        mock_api = MagicMock()
        mock_core_fn.return_value = mock_api

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod-abc"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_api.list_namespaced_pod.return_value = mock_pods
        mock_api.read_namespaced_pod_log.return_value = '{"result": "ok"}'

        result = kube.get_job_pod_log("test-job", "default")
        assert result == '{"result": "ok"}'

    @patch("agentit.kube.core_v1")
    def test_get_job_pod_log_no_pods(self, mock_core_fn):
        from agentit import kube

        mock_api = MagicMock()
        mock_core_fn.return_value = mock_api
        mock_pods = MagicMock()
        mock_pods.items = []
        mock_api.list_namespaced_pod.return_value = mock_pods

        result = kube.get_job_pod_log("test-job", "default")
        assert result == ""


class TestAgentRegistry:
    """Tests for the agent capability registry."""

    def test_all_agents_importable(self):
        from agentit.agents.capabilities import AGENT_CLASSES, get_agent_class

        for name in AGENT_CLASSES:
            cls = get_agent_class(name)
            assert cls is not None
            assert hasattr(cls, "run"), f"Agent class {name} missing run() method"

    def test_unknown_agent_raises(self):
        from agentit.agents.capabilities import get_agent_class

        with pytest.raises(ValueError, match="Unknown agent"):
            get_agent_class("nonexistent")

    def test_agent_classes_match_capabilities(self):
        """Every agent in AGENT_CLASSES has a matching entry in AGENT_CAPABILITIES."""
        from agentit.agents.capabilities import AGENT_CAPABILITIES, AGENT_CLASSES

        for name in AGENT_CLASSES:
            assert name in AGENT_CAPABILITIES, f"Agent {name} missing from AGENT_CAPABILITIES"

    def test_resource_tiers_defined(self):
        """Every agent has a valid resource tier."""
        from agentit.agents.capabilities import AGENT_CLASSES, RESOURCE_TIERS

        for name, (_cat, _mod, _cls, tier) in AGENT_CLASSES.items():
            assert tier in RESOURCE_TIERS, f"Agent {name} has unknown tier: {tier}"


class TestResultMarkers:
    """Tests for the result marker extraction used by K8s Job log parsing."""

    def test_extract_clean(self):
        payload = '[{"path": "a.yaml"}]'
        raw = f"--- AGENTIT_RESULT_BEGIN ---\n{payload}\n--- AGENTIT_RESULT_END ---"
        assert _extract_result(raw) == payload

    def test_extract_with_noise(self):
        payload = '[{"path": "a.yaml"}]'
        raw = f"WARNING: deprecation\nINFO: starting\n--- AGENTIT_RESULT_BEGIN ---\n{payload}\n--- AGENTIT_RESULT_END ---\n"
        assert _extract_result(raw) == payload

    def test_extract_no_markers_fallback(self):
        raw = '[{"path": "a.yaml"}]'
        assert _extract_result(raw) == raw


class TestAgentModeEnvVarFallback:
    """AGENTIT_AGENT_MODE is the documented/canonical env var; AGENT_MODE
    (the previously-undocumented name README.md and docs/architecture.md
    used to reference) is kept as a backward-compat fallback."""

    def test_agentit_agent_mode_takes_precedence(self, monkeypatch):
        from agentit.agents.orchestrator import _read_agent_mode

        monkeypatch.setenv("AGENTIT_AGENT_MODE", "kubernetes")
        monkeypatch.setenv("AGENT_MODE", "local")
        assert _read_agent_mode() == "kubernetes"

    def test_agent_mode_used_as_fallback_when_agentit_agent_mode_unset(self, monkeypatch):
        from agentit.agents.orchestrator import _read_agent_mode

        monkeypatch.delenv("AGENTIT_AGENT_MODE", raising=False)
        monkeypatch.setenv("AGENT_MODE", "kubernetes")
        assert _read_agent_mode() == "kubernetes"

    def test_defaults_to_local_when_neither_env_var_set(self, monkeypatch):
        from agentit.agents.orchestrator import _read_agent_mode

        monkeypatch.delenv("AGENTIT_AGENT_MODE", raising=False)
        monkeypatch.delenv("AGENT_MODE", raising=False)
        assert _read_agent_mode() == "local"
