"""Regression tests — every agent's generated YAML must pass validation."""

from __future__ import annotations

from pathlib import Path

import yaml

from agentit.agents.base import validate_manifest
from agentit.models import (
    AssessmentReport,
    DimensionScore,
    Finding,
    Severity,
)
from conftest import make_report


def _make_full_report(criticality: str = "medium", score: int = 30) -> AssessmentReport:
    """Build report with findings across all dimensions for agent validation tests."""
    return make_report(
        criticality=criticality,
        scores=[
            DimensionScore(
                dimension="security",
                score=score,
                max_score=100,
                findings=[
                    Finding(category="network", severity=Severity.high,
                            description="No NetworkPolicy", recommendation="Add NetworkPolicy"),
                    Finding(category="container", severity=Severity.high,
                            description="Running as root", recommendation="Add USER"),
                    Finding(category="secrets", severity=Severity.critical,
                            description="Hardcoded password", recommendation="Use secrets manager"),
                ],
            ),
            DimensionScore(dimension="observability", score=score, max_score=100, findings=[
                Finding(category="metrics", severity=Severity.medium,
                        description="No Prometheus", recommendation="Add metrics"),
            ]),
            DimensionScore(dimension="cicd", score=score, max_score=100, findings=[
                Finding(category="pipeline", severity=Severity.high,
                        description="No CI/CD", recommendation="Add pipeline"),
            ]),
            DimensionScore(dimension="compliance", score=score, max_score=100, findings=[
                Finding(category="policy", severity=Severity.medium,
                        description="No policies", recommendation="Add Kyverno"),
            ]),
            DimensionScore(dimension="infrastructure", score=score, max_score=100, findings=[]),
            DimensionScore(dimension="data_governance", score=score, max_score=100, findings=[]),
            DimensionScore(dimension="ha_dr", score=score, max_score=100, findings=[]),
        ],
    )


def _validate_agent_yaml(agent_cls: type, report: AssessmentReport, tmp_path: Path) -> list[str]:
    """Run an agent and validate all YAML output."""
    out = tmp_path / agent_cls.__name__
    agent = agent_cls(report=report, output_dir=out)
    result = agent.run()
    all_errors: list[str] = []
    non_k8s = {"dependabot.yml", "renovate.json"}
    for f in result.files:
        if not f.path.endswith((".yaml", ".yml")):
            continue
        if any(f.path.endswith(n) for n in non_k8s):
            continue
        errors = validate_manifest(f.content)
        for e in errors:
            all_errors.append(f"{agent_cls.__name__}/{f.path}: {e}")
    return all_errors


class TestAgentYamlValidity:
    def test_hardening_agent(self, tmp_path: Path) -> None:
        from agentit.agents.hardening import HardeningAgent
        errors = _validate_agent_yaml(HardeningAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Hardening agent produced invalid YAML: {errors}"

    def test_observability_agent(self, tmp_path: Path) -> None:
        from agentit.agents.observability import ObservabilityAgent
        errors = _validate_agent_yaml(ObservabilityAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Observability agent produced invalid YAML: {errors}"

    def test_cicd_agent(self, tmp_path: Path) -> None:
        from agentit.agents.cicd import CICDAgent
        errors = _validate_agent_yaml(CICDAgent, _make_full_report(), tmp_path)
        assert errors == [], f"CICD agent produced invalid YAML: {errors}"

    def test_compliance_agent(self, tmp_path: Path) -> None:
        from agentit.agents.compliance import ComplianceAgent
        errors = _validate_agent_yaml(ComplianceAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Compliance agent produced invalid YAML: {errors}"

    def test_cost_agent(self, tmp_path: Path) -> None:
        from agentit.agents.cost import CostOptimizationAgent
        errors = _validate_agent_yaml(CostOptimizationAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Cost agent produced invalid YAML: {errors}"

    def test_chaos_agent(self, tmp_path: Path) -> None:
        from agentit.agents.chaos import ChaosAgent
        errors = _validate_agent_yaml(ChaosAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Chaos agent produced invalid YAML: {errors}"

    def test_dependency_agent(self, tmp_path: Path) -> None:
        from agentit.agents.dependency import DependencyAgent
        errors = _validate_agent_yaml(DependencyAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Dependency agent produced invalid YAML: {errors}"

    def test_incident_agent(self, tmp_path: Path) -> None:
        from agentit.agents.incident import IncidentAgent
        errors = _validate_agent_yaml(IncidentAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Incident agent produced invalid YAML: {errors}"

    def test_retirement_agent(self, tmp_path: Path) -> None:
        from agentit.agents.retirement import RetirementAgent
        report = _make_full_report(score=10)
        errors = _validate_agent_yaml(RetirementAgent, report, tmp_path)
        assert errors == [], f"Retirement agent produced invalid YAML: {errors}"

    def test_release_agent(self, tmp_path: Path) -> None:
        from agentit.agents.release import ReleaseCoordinatorAgent
        errors = _validate_agent_yaml(ReleaseCoordinatorAgent, _make_full_report(), tmp_path)
        assert errors == [], f"Release agent produced invalid YAML: {errors}"
