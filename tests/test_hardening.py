from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from agentit.agents.hardening import HardeningAgent, HardeningResult
from agentit.models import (
    AssessmentReport,
    ArchitectureInfo,
    DimensionScore,
    Finding,
    Framework,
    Language,
    Severity,
    StackInfo,
)


def _make_report(
    *,
    repo_name: str = "test-app",
    languages: list[Language] | None = None,
    scores: list[DimensionScore] | None = None,
) -> AssessmentReport:
    if languages is None:
        languages = [Language(name="python", file_count=10, percentage=100.0)]
    return AssessmentReport(
        repo_url="https://github.com/org/test-app",
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


class TestNetworkPolicy:
    def test_generates_network_policy(self, tmp_path: Path) -> None:
        report = _make_report(
            scores=[_score_with_finding("security", "network", "No NetworkPolicy found")],
        )
        result = HardeningAgent(report, tmp_path / "out").run()

        np_files = [f for f in result.files if f.path == "network-policy.yaml"]
        assert len(np_files) == 1

        docs = list(yaml.safe_load_all(np_files[0].content))
        assert len(docs) == 2
        assert docs[0]["kind"] == "NetworkPolicy"
        assert docs[0]["metadata"]["name"] == "test-app-deny-all"
        assert docs[1]["spec"]["ingress"][0]["ports"][0]["port"] == 8080

    def test_no_network_policy_without_findings(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
        result = HardeningAgent(report, tmp_path / "out").run()
        assert not any(f.path == "network-policy.yaml" for f in result.files)


class TestContainerfile:
    def test_generates_containerfile_python(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            scores=[_score_with_finding("security", "container", "No Dockerfile found")],
        )
        result = HardeningAgent(report, tmp_path / "out").run()

        cf = [f for f in result.files if f.path == "Containerfile"]
        assert len(cf) == 1
        assert "ubi9/python-312" in cf[0].content
        assert (tmp_path / "out" / "Containerfile").exists()

    def test_generates_containerfile_go(self, tmp_path: Path) -> None:
        report = _make_report(
            languages=[Language(name="Go", file_count=5, percentage=100.0)],
            scores=[_score_with_finding("security", "container", "No Dockerfile found")],
        )
        result = HardeningAgent(report, tmp_path / "out").run()

        cf = [f for f in result.files if f.path == "Containerfile"]
        assert len(cf) == 1
        assert "go-toolset" in cf[0].content
        assert "ubi-minimal" in cf[0].content  # runtime stage

    def test_no_containerfile_without_findings(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
        result = HardeningAgent(report, tmp_path / "out").run()
        assert not any(f.path == "Containerfile" for f in result.files)


class TestRBAC:
    def test_generates_rbac(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
        result = HardeningAgent(report, tmp_path / "out").run()

        rbac = [f for f in result.files if f.path == "rbac.yaml"]
        assert len(rbac) == 1

        docs = list(yaml.safe_load_all(rbac[0].content))
        kinds = {d["kind"] for d in docs}
        assert kinds == {"ServiceAccount", "Role", "RoleBinding"}
        assert (tmp_path / "out" / "rbac.yaml").exists()


class TestSecurityContext:
    def test_generates_security_context(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
        result = HardeningAgent(report, tmp_path / "out").run()

        sc = [f for f in result.files if f.path == "security-context.yaml"]
        assert len(sc) == 1

        doc = yaml.safe_load(sc[0].content)
        assert doc["spec"]["securityContext"]["runAsNonRoot"] is True
        container_sc = doc["spec"]["containers"][0]["securityContext"]
        assert container_sc["readOnlyRootFilesystem"] is True
        assert container_sc["capabilities"]["drop"] == ["ALL"]


class TestResourceLimits:
    def test_generates_resource_limits(self, tmp_path: Path) -> None:
        report = _make_report(
            scores=[_score_with_finding("infrastructure", "resource", "No resource limits")],
        )
        result = HardeningAgent(report, tmp_path / "out").run()

        rl = [f for f in result.files if f.path == "resource-limits.yaml"]
        assert len(rl) == 1

        docs = list(yaml.safe_load_all(rl[0].content))
        kinds = {d["kind"] for d in docs}
        assert kinds == {"ResourceQuota", "LimitRange"}

    def test_no_resource_limits_without_findings(self, tmp_path: Path) -> None:
        report = _make_report(scores=[])
        result = HardeningAgent(report, tmp_path / "out").run()
        assert not any(f.path == "resource-limits.yaml" for f in result.files)


class TestCleanReport:
    def test_generates_nothing_for_clean_report(self, tmp_path: Path) -> None:
        """With no findings, only unconditional generators (RBAC, security-context) fire."""
        report = _make_report(scores=[])
        result = HardeningAgent(report, tmp_path / "out").run()

        paths = {f.path for f in result.files}
        assert "network-policy.yaml" not in paths
        assert "Containerfile" not in paths
        assert "resource-limits.yaml" not in paths
        # RBAC and security-context are always generated
        assert "rbac.yaml" in paths
        assert "security-context.yaml" in paths


class TestOutputDir:
    def test_output_dir_created(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "deep" / "dir"
        assert not out.exists()
        report = _make_report(scores=[])
        HardeningAgent(report, out).run()
        assert out.exists()
        assert out.is_dir()
