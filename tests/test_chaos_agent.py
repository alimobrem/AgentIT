from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from conftest import make_report

from agentit.agents.chaos import ChaosAgent, ChaosResult


class TestPodKill:
    def test_generates_pod_kill_experiment(self, tmp_path: Path) -> None:
        report = make_report()
        result = ChaosAgent(report, tmp_path / "out").run()

        pk = [f for f in result.files if f.path == "chaos-pod-kill.yaml"]
        assert len(pk) == 1

        doc = yaml.safe_load(pk[0].content)
        assert doc["kind"] == "ChaosEngine"
        assert doc["metadata"]["name"] == "test-app-pod-kill"
        assert doc["spec"]["appinfo"]["applabel"] == "app=test-app"

        exp = doc["spec"]["experiments"][0]
        assert exp["name"] == "pod-kill"
        env = {e["name"]: e["value"] for e in exp["spec"]["components"]["env"]}
        assert env["KILL_COUNT"] == "1"
        assert env["TOTAL_CHAOS_DURATION"] == "60"

        probe = exp["spec"]["probe"][0]
        assert probe["runProperties"]["probeTimeout"] == "60s"

        assert (tmp_path / "out" / "chaos-pod-kill.yaml").exists()


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

    def test_skips_schedule_for_critical_apps(self, tmp_path: Path) -> None:
        report = make_report(criticality="critical")
        result = ChaosAgent(report, tmp_path / "out").run()

        assert not any(f.path == "chaos-schedule.yaml" for f in result.files)
        assert len(result.files) == 3  # pod-kill, network-latency, cpu-stress only
