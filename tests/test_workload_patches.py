"""Workload replicas / health-probe source patches + clear-evidence."""
from __future__ import annotations

from agentit.remediation.clear_evidence import (
    WORKLOAD_PROBES,
    WORKLOAD_REPLICAS,
    verify_evidence,
)
from agentit.remediation.source_patches import harden_dockerfile_content
from agentit.remediation.workload_patches import (
    has_health_probes,
    patch_health_probes,
    patch_replicas,
    replicas_at_least,
    verify_workload_probes,
    verify_workload_replicas,
)


class TestWorkloadReplicas:
    def test_bumps_replicas_1_to_2(self) -> None:
        src = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: app\n"
            "spec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: app\n"
        )
        out = patch_replicas(src, replicas=2)
        assert replicas_at_least(out, 2)
        assert "replicas: 2" in out

    def test_clear_evidence_ok(self) -> None:
        ok, reason = verify_workload_replicas([{
            "target_path": "deploy/deployment.yaml",
            "content": (
                "kind: Deployment\nspec:\n  replicas: 2\n"
            ),
        }])
        assert ok, reason
        ok2, _ = verify_evidence(WORKLOAD_REPLICAS, [{
            "target_path": "deploy/deployment.yaml",
            "content": "kind: Rollout\nspec:\n  replicas: 3\n",
        }])
        assert ok2


class TestWorkloadProbes:
    def test_injects_probes(self) -> None:
        src = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: app\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          image: app:1\n"
            "          ports:\n"
            "            - containerPort: 8080\n"
        )
        out = patch_health_probes(src)
        assert has_health_probes(out)
        assert "livenessProbe" in out and "readinessProbe" in out

    def test_clear_evidence_ok(self) -> None:
        content = (
            "kind: Deployment\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          livenessProbe:\n"
            "            tcpSocket:\n"
            "              port: 8080\n"
            "          readinessProbe:\n"
            "            tcpSocket:\n"
            "              port: 8080\n"
        )
        ok, reason = verify_workload_probes([{
            "target_path": "deploy/deployment.yaml",
            "content": content,
        }])
        assert ok, reason
        ok2, _ = verify_evidence(WORKLOAD_PROBES, [{
            "target_path": "chart/templates/deploy.yaml",
            "content": content,
        }])
        assert ok2


class TestDockerfileHarden:
    def test_adds_user_and_healthcheck(self) -> None:
        existing = "FROM python:latest\nWORKDIR /app\nCOPY . .\n"
        out = harden_dockerfile_content(
            existing,
            add_user=True,
            add_healthcheck=True,
            force_ubi=True,
            language="python",
        )
        assert "USER 1001" in out
        assert "HEALTHCHECK" in out
        assert "ubi" in out.lower()
        assert ":latest" not in out.split("FROM", 1)[-1].splitlines()[0]
