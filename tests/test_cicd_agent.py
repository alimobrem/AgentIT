from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from agentit.agents.cicd import CICDAgent, CICDResult
from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)


def _make_report(
    *,
    repo_name: str = "test-app",
    repo_url: str = "https://github.com/org/test-app",
    languages: list[Language] | None = None,
    scores: list[DimensionScore] | None = None,
) -> AssessmentReport:
    if languages is None:
        languages = [Language(name="python", file_count=10, percentage=100.0)]
    return AssessmentReport(
        repo_url=repo_url,
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=languages,
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
        ),
        scores=scores or [],
        criticality="medium",
        summary="test summary",
        remediation_plan=[],
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
    def test_generates_tekton_pipeline(self, tmp_path: Path) -> None:
        report = _make_report(
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

        pipeline_run = docs[1]
        assert pipeline_run["kind"] == "PipelineRun"
        assert (tmp_path / "out" / "tekton-pipeline.yaml").exists()

    def test_skips_tekton_without_findings(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
        result = CICDAgent(report, tmp_path / "out").run()
        assert not any(f.path == "tekton-pipeline.yaml" for f in result.files)


class TestArgoCDApplication:
    def test_generates_argocd_application(self, tmp_path: Path) -> None:
        repo_url = "https://github.com/org/my-service"
        report = _make_report(
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
        report = _make_report(scores=[])
        result = CICDAgent(report, tmp_path / "out").run()
        assert not any(f.path == "argocd-application.yaml" for f in result.files)


class TestQuayConfig:
    def test_generates_quay_config(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
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

        report = _make_report(
            scores=[_score_with_finding("security", "container", "No Dockerfile found")],
        )
        result = CICDAgent(report, out).run()
        assert not any(f.path == "Containerfile" for f in result.files)
