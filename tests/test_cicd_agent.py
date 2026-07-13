from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from conftest import make_report

from agentit.agents.cicd import CICDAgent, CICDResult
from agentit.models import (
    DimensionScore,
    Finding,
    Language,
    Severity,
)


def _score_with_finding(dimension: str, category: str, desc: str) -> DimensionScore:
    return DimensionScore(
        dimension=dimension,
        score=30,
        max_score=100,
        findings=[
            Finding(
                category=category,
                severity=Severity.high,
                description=desc,
                recommendation="fix it",
            ),
        ],
    )


class TestTektonPipeline:
    def test_generates_tekton_pipeline_minimal_without_scan_or_sbom_findings(self, tmp_path: Path) -> None:
        """Without scanning/sbom findings, HardeningAgent/ComplianceAgent never
        generate the {name}-image-scan / {name}-sbom-generate Tekton Tasks, so
        the pipeline must not reference them either (dangling taskRef fails at
        PipelineRun time)."""
        report = make_report(
            scores=[_score_with_finding("cicd", "pipeline", "No CI/CD pipeline found")],
        )
        result = CICDAgent(report, tmp_path / "out").run()

        tp = [f for f in result.files if f.path == "tekton-pipeline.yaml"]
        assert len(tp) == 1

        docs = list(yaml.safe_load_all(tp[0].content))
        assert len(docs) == 2

        pipeline = docs[0]
        assert pipeline["kind"] == "Pipeline"
        task_names = [t["name"] for t in pipeline["spec"]["tasks"]]
        assert task_names == ["git-clone", "build", "test", "image-build", "image-push", "deploy"]

        deploy_task = next(t for t in pipeline["spec"]["tasks"] if t["name"] == "deploy")
        assert deploy_task["runAfter"] == ["image-push"]

        pipeline_run = docs[1]
        assert pipeline_run["kind"] == "PipelineRun"
        assert (tmp_path / "out" / "tekton-pipeline.yaml").exists()

    def test_generates_tekton_pipeline_with_scan_and_sbom_findings(self, tmp_path: Path) -> None:
        """When scanning and SBOM findings are present (so HardeningAgent and
        ComplianceAgent will actually generate those Tekton Tasks), the
        pipeline includes and gates deploy on both."""
        report = make_report(
            scores=[
                _score_with_finding("cicd", "pipeline", "No CI/CD pipeline found"),
                _score_with_finding("security", "vulnerability", "No image scanning"),
                _score_with_finding("compliance", "sbom", "No SBOM found"),
            ],
        )
        result = CICDAgent(report, tmp_path / "out").run()

        tp = [f for f in result.files if f.path == "tekton-pipeline.yaml"]
        assert len(tp) == 1

        pipeline = list(yaml.safe_load_all(tp[0].content))[0]
        task_names = [t["name"] for t in pipeline["spec"]["tasks"]]
        assert task_names == ["git-clone", "build", "test", "image-build", "image-push", "image-scan", "sbom-generate", "deploy"]

        deploy_task = next(t for t in pipeline["spec"]["tasks"] if t["name"] == "deploy")
        assert set(deploy_task["runAfter"]) == {"image-scan", "sbom-generate"}

    def test_scan_task_only_included_with_scanning_finding(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[
                _score_with_finding("cicd", "pipeline", "No CI/CD pipeline found"),
                _score_with_finding("security", "cve", "Critical CVE detected"),
            ],
        )
        result = CICDAgent(report, tmp_path / "out").run()
        pipeline = list(yaml.safe_load_all(
            [f for f in result.files if f.path == "tekton-pipeline.yaml"][0].content
        ))[0]
        task_names = {t["name"] for t in pipeline["spec"]["tasks"]}
        assert "image-scan" in task_names
        assert "sbom-generate" not in task_names

    def test_skips_tekton_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = CICDAgent(report, tmp_path / "out").run()
        assert not any(f.path == "tekton-pipeline.yaml" for f in result.files)


class TestArgoCDApplication:
    def test_generates_argocd_application(self, tmp_path: Path) -> None:
        repo_url = "https://github.com/org/my-service"
        report = make_report(
            repo_url=repo_url,
            scores=[_score_with_finding("deployment", "gitops", "No GitOps configuration found")],
        )
        result = CICDAgent(report, tmp_path / "out").run()

        argo = [f for f in result.files if f.path == "argocd-application.yaml"]
        assert len(argo) == 1

        doc = yaml.safe_load(argo[0].content)
        assert doc["kind"] == "Application"
        assert doc["spec"]["source"]["repoURL"] == repo_url
        assert doc["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
        assert doc["spec"]["syncPolicy"]["automated"]["prune"] is True
        assert (tmp_path / "out" / "argocd-application.yaml").exists()

    def test_skips_argocd_without_findings(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = CICDAgent(report, tmp_path / "out").run()
        assert not any(f.path == "argocd-application.yaml" for f in result.files)


class TestArgoRollout:
    def test_rollout_uses_kubernetes_recommended_labels(self, tmp_path: Path) -> None:
        """The Rollout's selector/pod-template labels and its Services'
        selectors must all use app.kubernetes.io/name — matching
        InfrastructureAgent's HPA/PDB matchLabels — otherwise the HPA/PDB
        generated for this app are inert against the real workload."""
        report = make_report()
        result = CICDAgent(report, tmp_path / "out").run()

        rollout_files = [f for f in result.files if f.path == "argo-rollout.yaml"]
        assert len(rollout_files) == 1

        docs = list(yaml.safe_load_all(rollout_files[0].content))
        rollout = next(d for d in docs if d["kind"] == "Rollout")
        assert rollout["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": "test-app"}
        assert rollout["spec"]["template"]["metadata"]["labels"] == {"app.kubernetes.io/name": "test-app"}
        assert "app" not in rollout["spec"]["selector"]["matchLabels"]

        services = [d for d in docs if d["kind"] == "Service"]
        assert len(services) == 2
        for svc in services:
            assert svc["spec"]["selector"] == {"app.kubernetes.io/name": "test-app"}


class TestQuayConfig:
    def test_generates_quay_config(self, tmp_path: Path) -> None:
        report = make_report(scores=[])
        result = CICDAgent(report, tmp_path / "out").run()

        qc = [f for f in result.files if f.path == "quay-config.yaml"]
        assert len(qc) == 1

        doc = yaml.safe_load(qc[0].content)
        assert doc["data"]["image-scanning"] == "enabled"
        assert doc["data"]["vulnerability-notifications"] == "enabled"
        assert (tmp_path / "out" / "quay-config.yaml").exists()


class TestContainerfileSkip:
    def test_skips_containerfile_if_already_exists(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "Containerfile").write_text("FROM scratch\n")

        report = make_report(
            scores=[_score_with_finding("security", "container", "No Dockerfile found")],
        )
        result = CICDAgent(report, out).run()
        assert not any(f.path == "Containerfile" for f in result.files)


class TestApplicationSet:
    def test_generates_applicationset_with_infra_repo(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("cicd", "gitops", "No GitOps deployment")],
        )
        report.infra_repo_url = "https://github.com/org/gitops-infra"
        result = CICDAgent(report, tmp_path / "out").run()

        appset = [f for f in result.files if f.path == "argocd-applicationset.yaml"]
        assert len(appset) == 1

        doc = yaml.safe_load(appset[0].content)
        assert doc["kind"] == "ApplicationSet"
        assert doc["spec"]["generators"][0]["git"]["repoURL"] == "https://github.com/org/gitops-infra"
        assert doc["spec"]["generators"][0]["git"]["directories"][0]["path"] == "apps/*"
        assert "{{path.basename}}" in doc["spec"]["template"]["metadata"]["name"]

    def test_generates_application_without_infra_repo(self, tmp_path: Path) -> None:
        report = make_report(
            scores=[_score_with_finding("cicd", "gitops", "No GitOps deployment")],
        )
        result = CICDAgent(report, tmp_path / "out").run()

        app = [f for f in result.files if f.path == "argocd-application.yaml"]
        assert len(app) == 1

        doc = yaml.safe_load(app[0].content)
        assert doc["kind"] == "Application"
