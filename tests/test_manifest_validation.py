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
    # Cost/dependency Python agents removed (skills own remediations).
    # Skill YAML validity: tests/test_skill_agent_parity.py.
    # Optional codechange may emit non-YAML source patches — covered by
    # tests/test_codechange_agent.py (if present) / orchestrator runs.

    def test_codechange_agent_yaml_outputs_valid_when_present(self, tmp_path: Path) -> None:
        from agentit.agents.codechange import CodeChangeAgent
        errors = _validate_agent_yaml(CodeChangeAgent, _make_full_report(), tmp_path)
        assert errors == [], f"CodeChange agent produced invalid YAML: {errors}"
