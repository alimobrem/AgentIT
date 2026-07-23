"""CI-SBOM detection and sbom-ci source patch."""
from __future__ import annotations

from pathlib import Path

from agentit.models import DimensionScore, Finding, Severity
from agentit.remediation.clear_evidence import verify_sbom_ci
from agentit.remediation.sbom_ci import (
    content_has_gha_or_ci_sbom,
    content_has_tekton_pipeline_sbom,
    content_is_bare_sbom_task,
    default_gha_sbom_workflow,
    repo_has_ci_sbom_generation,
)
from agentit.remediation.source_patches import generate_source_patch_for_skill
from agentit.skill_engine import load_skill
from conftest import make_report

SKILLS = Path(__file__).resolve().parent.parent / "skills"


def test_gha_action_detected():
    assert content_has_gha_or_ci_sbom(
        "uses: anchore/sbom-action@v0.24.0\n"
    )


def test_dot_github_workflows_path_is_ci():
    from agentit.remediation.sbom_ci import is_ci_config_path

    assert is_ci_config_path(".github/workflows/sbom.yml")
    assert is_ci_config_path("./.github/workflows/security.yaml")
    assert not is_ci_config_path("docs/sbom.yml")


def test_syft_cyclonedx_in_ci_detected():
    assert content_has_gha_or_ci_sbom(
        "run: syft . -o cyclonedx-json=sbom.json\n"
    )


def test_pipeline_wire_detected():
    body = (
        "kind: Pipeline\n"
        "spec:\n  tasks:\n"
        "  - name: sbom-generate\n"
        "    taskRef:\n      name: app-sbom\n"
    )
    assert content_has_tekton_pipeline_sbom(body)
    assert not content_is_bare_sbom_task(body)


def test_bare_task_is_wrong_layer():
    body = (
        "kind: Task\n"
        "spec:\n  steps:\n"
        "  - image: anchore/syft:v1.48.0\n"
        "    args: [img, --output, cyclonedx-json=/ws/sbom.json]\n"
    )
    assert content_is_bare_sbom_task(body)
    assert not content_has_tekton_pipeline_sbom(body)


def test_repo_has_ci_sbom_from_workflow(create_mock_repo):
    repo = create_mock_repo({
        ".github/workflows/security.yml": default_gha_sbom_workflow(),
    })
    assert repo_has_ci_sbom_generation(repo)


def test_repo_static_file_alone_false(create_mock_repo):
    repo = create_mock_repo({
        "sbom.cdx.json": '{"bomFormat":"CycloneDX","components":[{"name":"x"}]}\n',
    })
    assert not repo_has_ci_sbom_generation(repo)


def test_sbom_ci_skill_generates_workflow():
    skill = load_skill(SKILLS / "compliance" / "sbom-ci.md")
    assert skill is not None
    report = make_report(scores=[DimensionScore(
        dimension="compliance", score=40, max_score=100,
        findings=[Finding(
            category="sbom", severity=Severity.high,
            description="No SBOM generation in CI",
            recommendation="Add CI SBOM",
        )],
    )])
    files = generate_source_patch_for_skill(skill, report, "app")
    assert files
    assert files[0].target_path == ".github/workflows/sbom.yml"
    assert "anchore/sbom-action" in files[0].content
    ok, reason = verify_sbom_ci([{
        "target_path": files[0].target_path,
        "content": files[0].content,
        "skill_name": "sbom-ci",
    }])
    assert ok, reason
