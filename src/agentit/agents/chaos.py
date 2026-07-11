from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from agentit.agents.base import GeneratedFile, _sanitize_name
from agentit.models import AssessmentReport


class ChaosResult(BaseModel):
    files: list[GeneratedFile]
    summary: str = ""

    def model_post_init(self, _context: object) -> None:
        count = len(self.files)
        self.summary = (
            f"Generated {count} chaos experiment{'s' if count != 1 else ''}."
        )


class ChaosAgent:
    def __init__(self, report: AssessmentReport, output_dir: Path) -> None:
        self.report = report
        self.output_dir = Path(output_dir)
        self._name = _sanitize_name(report.repo_name)

    def run(self) -> ChaosResult:
        """Generate chaos engineering experiment configs."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[GeneratedFile] = []

        generated.append(self._generate_pod_kill())
        generated.append(self._generate_network_latency())
        generated.append(self._generate_cpu_stress())

        schedule = self._generate_schedule()
        if schedule is not None:
            generated.append(schedule)

        return ChaosResult(files=generated)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _write(self, filename: str, content: str) -> None:
        (self.output_dir / filename).write_text(content)

    # ------------------------------------------------------------------
    # generators
    # ------------------------------------------------------------------

    def _generate_pod_kill(self) -> GeneratedFile:
        name = self._name
        doc = {
            "apiVersion": "litmuschaos.io/v1alpha1",
            "kind": "ChaosEngine",
            "metadata": {
                "name": f"{name}-pod-kill",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "appinfo": {
                    "appns": "default",
                    "applabel": f"app={name}",
                    "appkind": "deployment",
                },
                "engineState": "active",
                "chaosServiceAccount": f"{name}-chaos-sa",
                "experiments": [
                    {
                        "name": "pod-kill",
                        "spec": {
                            "components": {
                                "env": [
                                    {"name": "TOTAL_CHAOS_DURATION", "value": "60"},
                                    {"name": "CHAOS_INTERVAL", "value": "10"},
                                    {"name": "KILL_COUNT", "value": "1"},
                                ],
                            },
                            "probe": [
                                {
                                    "name": "check-pod-recovery",
                                    "type": "k8sProbe",
                                    "mode": "EOT",
                                    "k8sProbe/inputs": {
                                        "command": {
                                            "group": "",
                                            "version": "v1",
                                            "resource": "pods",
                                            "namespace": "default",
                                            "fieldSelector": f"status.phase=Running,metadata.labels.app={name}",
                                        },
                                    },
                                    "runProperties": {
                                        "probeTimeout": "60s",
                                        "retry": 3,
                                        "interval": "5s",
                                    },
                                },
                            ],
                        },
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("chaos-pod-kill.yaml", content)

        return GeneratedFile(
            path="chaos-pod-kill.yaml",
            content=content,
            description="LitmusChaos ChaosEngine: kill 1 pod, verify recovery within 60s.",
        )

    def _generate_network_latency(self) -> GeneratedFile:
        name = self._name
        doc = {
            "apiVersion": "litmuschaos.io/v1alpha1",
            "kind": "ChaosEngine",
            "metadata": {
                "name": f"{name}-network-latency",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "appinfo": {
                    "appns": "default",
                    "applabel": f"app={name}",
                    "appkind": "deployment",
                },
                "engineState": "active",
                "chaosServiceAccount": f"{name}-chaos-sa",
                "experiments": [
                    {
                        "name": "pod-network-latency",
                        "spec": {
                            "components": {
                                "env": [
                                    {"name": "TOTAL_CHAOS_DURATION", "value": "60"},
                                    {"name": "NETWORK_LATENCY", "value": "500"},
                                    {"name": "NETWORK_INTERFACE", "value": "eth0"},
                                ],
                            },
                            "probe": [
                                {
                                    "name": "check-app-responsive",
                                    "type": "httpProbe",
                                    "mode": "Continuous",
                                    "httpProbe/inputs": {
                                        "url": f"http://{name}.default.svc:8080/healthz",
                                        "method": {"get": {"criteria": "==", "responseCode": "200"}},
                                    },
                                    "runProperties": {
                                        "probeTimeout": "5s",
                                        "retry": 3,
                                        "interval": "10s",
                                    },
                                },
                            ],
                        },
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("chaos-network-latency.yaml", content)

        return GeneratedFile(
            path="chaos-network-latency.yaml",
            content=content,
            description="Network latency injection: 500ms for 60s, verify app stays responsive.",
        )

    def _generate_cpu_stress(self) -> GeneratedFile:
        name = self._name
        doc = {
            "apiVersion": "litmuschaos.io/v1alpha1",
            "kind": "ChaosEngine",
            "metadata": {
                "name": f"{name}-cpu-stress",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "appinfo": {
                    "appns": "default",
                    "applabel": f"app={name}",
                    "appkind": "deployment",
                },
                "engineState": "active",
                "chaosServiceAccount": f"{name}-chaos-sa",
                "experiments": [
                    {
                        "name": "pod-cpu-hog",
                        "spec": {
                            "components": {
                                "env": [
                                    {"name": "TOTAL_CHAOS_DURATION", "value": "120"},
                                    {"name": "CPU_CORES", "value": "1"},
                                    {"name": "CPU_LOAD", "value": "80"},
                                ],
                            },
                        },
                    },
                ],
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("chaos-cpu-stress.yaml", content)

        return GeneratedFile(
            path="chaos-cpu-stress.yaml",
            content=content,
            description="CPU stress: 1 core at 80% for 120s to verify HPA scaling.",
        )

    def _generate_schedule(self) -> GeneratedFile | None:
        if self.report.criticality == "critical":
            return None

        name = self._name
        doc = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": f"{name}-chaos-schedule",
                "namespace": "default",
                "labels": {"app.kubernetes.io/name": name},
            },
            "spec": {
                "schedule": "0 2 * * 3",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "serviceAccountName": f"{name}-chaos-sa",
                                "restartPolicy": "Never",
                                "containers": [
                                    {
                                        "name": "chaos-runner",
                                        "image": "litmuschaos/litmus-checker:latest",
                                        "args": [
                                            "--chaosengine",
                                            f"{name}-pod-kill",
                                            "--namespace",
                                            "default",
                                        ],
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        }

        content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
        self._write("chaos-schedule.yaml", content)

        return GeneratedFile(
            path="chaos-schedule.yaml",
            content=content,
            description="CronJob: run chaos experiments weekly (Wednesday 2am).",
        )
