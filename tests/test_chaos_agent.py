from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
import pytest

from conftest import make_report

from agentit.agents.chaos import ChaosAgent, ChaosResult


class TestPodKill:
    def test_generates_pod_delete_experiment(self, tmp_path: Path) -> None:
        report = make_report()
        result = ChaosAgent(report, tmp_path / "out").run()

        pk = [f for f in result.files if f.path == "chaos-pod-delete.yaml"]
        assert len(pk) == 1

        doc = yaml.safe_load(pk[0].content)
        assert doc["kind"] == "ChaosEngine"
        assert doc["metadata"]["name"] == "test-app-pod-delete"
        assert doc["spec"]["appinfo"]["applabel"] == "app=test-app"

        exp = doc["spec"]["experiments"][0]
        # "pod-delete" is the actual registered LitmusChaos experiment name —
        # "pod-kill" is not a valid ChaosExperiment CRD.
        assert exp["name"] == "pod-delete"
        env = {e["name"]: e["value"] for e in exp["spec"]["components"]["env"]}
        assert "KILL_COUNT" not in env
        assert env["PODS_AFFECTED_PERC"] == "0"
        assert env["TOTAL_CHAOS_DURATION"] == "60"

        probe = exp["spec"]["probe"][0]
        assert probe["runProperties"]["probeTimeout"] == "60s"
        # Litmus's k8sProbe needs labelSelector for label matches —
        # fieldSelector only supports resource fields like status.phase.
        command = probe["k8sProbe/inputs"]["command"]
        assert command["fieldSelector"] == "status.phase=Running"
        assert command["labelSelector"] == "app=test-app"

        assert (tmp_path / "out" / "chaos-pod-delete.yaml").exists()


class TestNetworkLatency:
    def test_generates_network_latency(self, tmp_path: Path) -> None:
        report = make_report()
        result = ChaosAgent(report, tmp_path / "out").run()

        nl = [f for f in result.files if f.path == "chaos-network-latency.yaml"]
        assert len(nl) == 1

        doc = yaml.safe_load(nl[0].content)
        assert doc["kind"] == "ChaosEngine"
        assert doc["metadata"]["name"] == "test-app-network-latency"

        exp = doc["spec"]["experiments"][0]
        assert exp["name"] == "pod-network-latency"
        env = {e["name"]: e["value"] for e in exp["spec"]["components"]["env"]}
        assert env["NETWORK_LATENCY"] == "500"
        assert env["TOTAL_CHAOS_DURATION"] == "60"

        probe = exp["spec"]["probe"][0]
        assert probe["type"] == "httpProbe"
        assert probe["mode"] == "Continuous"

        assert (tmp_path / "out" / "chaos-network-latency.yaml").exists()


class TestCpuStress:
    def test_generates_cpu_stress(self, tmp_path: Path) -> None:
        report = make_report()
        result = ChaosAgent(report, tmp_path / "out").run()

        cs = [f for f in result.files if f.path == "chaos-cpu-stress.yaml"]
        assert len(cs) == 1

        doc = yaml.safe_load(cs[0].content)
        assert doc["kind"] == "ChaosEngine"

        exp = doc["spec"]["experiments"][0]
        assert exp["name"] == "pod-cpu-hog"
        env = {e["name"]: e["value"] for e in exp["spec"]["components"]["env"]}
        assert env["CPU_CORES"] == "1"
        assert env["CPU_LOAD"] == "80"
        assert env["TOTAL_CHAOS_DURATION"] == "120"

        assert (tmp_path / "out" / "chaos-cpu-stress.yaml").exists()


class TestAgentRegistration:
    def test_chaos_registered_in_agent_classes(self) -> None:
        """ChaosAgent is documented in README.md, has its own module with
        tests, and is referenced by schedules.py ('chaos' job key) — it must
        actually be registered, or it's completely dead in production."""
        from agentit.agents.capabilities import AGENT_CLASSES, get_agent_class

        assert "chaos" in AGENT_CLASSES
        category, module_path, class_name, _tier = AGENT_CLASSES["chaos"]
        assert module_path == "agentit.agents.chaos"
        assert class_name == "ChaosAgent"
        assert get_agent_class("chaos") is ChaosAgent

    def test_chaos_registered_in_agent_capabilities(self) -> None:
        from agentit.agents.capabilities import AGENT_CAPABILITIES

        assert "chaos" in AGENT_CAPABILITIES


class TestManifestValidationWiring:
    def test_generated_manifests_are_validated(self, tmp_path: Path) -> None:
        """ChaosAgent must run its generated YAML through validate_manifest()
        (like InfrastructureAgent does) so future schema regressions are
        caught rather than silently shipped."""
        report = make_report()
        with patch("agentit.agents.chaos.validate_manifest", return_value=[]) as mock_validate:
            ChaosAgent(report, tmp_path / "out").run()
        assert mock_validate.call_count >= 3  # pod-delete, network-latency, cpu-stress


class TestSchedule:
    def test_generates_cronjob_for_non_critical(self, tmp_path: Path) -> None:
        report = make_report(criticality="medium")
        result = ChaosAgent(report, tmp_path / "out").run()

        sched = [f for f in result.files if f.path == "chaos-schedule.yaml"]
        assert len(sched) == 1

        doc = yaml.safe_load(sched[0].content)
        assert doc["kind"] == "CronJob"
        assert doc["apiVersion"] == "batch/v1"
        assert doc["spec"]["schedule"] == "0 2 * * 3"
        assert doc["spec"]["concurrencyPolicy"] == "Forbid"

        # Must reference the ChaosEngine's actual metadata.name (test-app-pod-delete),
        # not the old test-app-pod-kill.
        command = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
        assert "test-app-pod-delete" in command
        assert "test-app-pod-kill" not in command

    def test_skips_schedule_for_critical_apps(self, tmp_path: Path) -> None:
        report = make_report(criticality="critical")
        result = ChaosAgent(report, tmp_path / "out").run()

        assert not any(f.path == "chaos-schedule.yaml" for f in result.files)
        assert len(result.files) == 3  # pod-kill, network-latency, cpu-stress only
