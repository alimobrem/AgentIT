"""Tests for the Infrastructure Agent (HPA, PDB, ResourceQuota, LimitRange, Namespace)."""

from __future__ import annotations

from pathlib import Path

import yaml

from conftest import make_report

from agentit.agents.infrastructure import InfrastructureAgent, InfrastructureResult


class TestHPA:
    def test_generates_hpa(self, tmp_path: Path) -> None:
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()

        hpa = [f for f in result.files if f.path == "hpa.yaml"]
        assert len(hpa) == 1

        doc = yaml.safe_load(hpa[0].content)
        assert doc["kind"] == "HorizontalPodAutoscaler"
        assert doc["spec"]["minReplicas"] == 2
        assert doc["spec"]["maxReplicas"] == 10
        assert (tmp_path / "out" / "hpa.yaml").exists()

    def test_hpa_targets_rollout_by_default(self, tmp_path: Path) -> None:
        """CICDAgent always generates an Argo Rollout (never a plain
        Deployment) as the workload — the HPA must scaleTargetRef that kind
        or it's inert against the real deployed resource."""
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()
        doc = yaml.safe_load([f for f in result.files if f.path == "hpa.yaml"][0].content)
        assert doc["spec"]["scaleTargetRef"]["kind"] == "Rollout"
        assert doc["spec"]["scaleTargetRef"]["apiVersion"] == "argoproj.io/v1alpha1"

    def test_hpa_targets_deployment_when_rollouts_disabled(self, tmp_path: Path) -> None:
        report = make_report()
        result = InfrastructureAgent(
            report, tmp_path / "out", uses_argo_rollouts=False
        ).run()
        doc = yaml.safe_load([f for f in result.files if f.path == "hpa.yaml"][0].content)
        assert doc["spec"]["scaleTargetRef"]["kind"] == "Deployment"
        assert doc["spec"]["scaleTargetRef"]["apiVersion"] == "apps/v1"

    def test_hpa_matchlabels_use_kubernetes_recommended_convention(self, tmp_path: Path) -> None:
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()
        doc = yaml.safe_load([f for f in result.files if f.path == "hpa.yaml"][0].content)
        assert doc["metadata"]["labels"] == {"app.kubernetes.io/name": "test-app"}


class TestPDB:
    def test_generates_pdb(self, tmp_path: Path) -> None:
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()

        pdb = [f for f in result.files if f.path == "pdb.yaml"]
        assert len(pdb) == 1

        doc = yaml.safe_load(pdb[0].content)
        assert doc["kind"] == "PodDisruptionBudget"
        assert doc["spec"]["minAvailable"] == 1
        assert (tmp_path / "out" / "pdb.yaml").exists()

    def test_pdb_selector_matches_hpa_and_rollout_convention(self, tmp_path: Path) -> None:
        """PDB's selector must use app.kubernetes.io/name to match the pod
        template labels that CICDAgent's/ReleaseCoordinatorAgent's Argo
        Rollout actually applies to pods — otherwise the PDB never selects
        any pods."""
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()
        doc = yaml.safe_load([f for f in result.files if f.path == "pdb.yaml"][0].content)
        assert doc["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": "test-app"}
        assert "app" not in doc["spec"]["selector"]["matchLabels"]


class TestResourceQuota:
    def test_generates_resourcequota_by_criticality(self, tmp_path: Path) -> None:
        report = make_report(criticality="high")
        result = InfrastructureAgent(report, tmp_path / "out").run()

        rq = [f for f in result.files if f.path == "resourcequota.yaml"]
        assert len(rq) == 1
        doc = yaml.safe_load(rq[0].content)
        assert doc["kind"] == "ResourceQuota"
        assert doc["spec"]["hard"]["limits.cpu"] == "16"
        assert (tmp_path / "out" / "resourcequota.yaml").exists()


class TestLimitRange:
    def test_generates_limitrange(self, tmp_path: Path) -> None:
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()

        lr = [f for f in result.files if f.path == "limitrange.yaml"]
        assert len(lr) == 1
        doc = yaml.safe_load(lr[0].content)
        assert doc["kind"] == "LimitRange"
        assert (tmp_path / "out" / "limitrange.yaml").exists()


class TestNamespace:
    def test_generates_namespace(self, tmp_path: Path) -> None:
        report = make_report(criticality="critical")
        result = InfrastructureAgent(report, tmp_path / "out").run()

        ns = [f for f in result.files if f.path == "namespace.yaml"]
        assert len(ns) == 1
        doc = yaml.safe_load(ns[0].content)
        assert doc["kind"] == "Namespace"
        assert doc["metadata"]["labels"]["agentit/criticality"] == "critical"
        assert (tmp_path / "out" / "namespace.yaml").exists()


class TestInfrastructureResult:
    def test_summary_count(self, tmp_path: Path) -> None:
        report = make_report()
        result = InfrastructureAgent(report, tmp_path / "out").run()
        assert isinstance(result, InfrastructureResult)
        assert "5 infrastructure manifests" in result.summary
        assert len(result.files) == 5
