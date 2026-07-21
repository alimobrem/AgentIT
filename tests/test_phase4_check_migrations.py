"""Phase 4 parity tests: `checks/*.yaml` -> `mode: detect` skill ports.

Each class here proves one ported skill produces exactly the same
`Finding` its deleted `checks/*.yaml` counterpart used to (same category/
severity/description/recommendation, firing under the same conditions and
passing under the same conditions) -- the same discipline Phase 1
established for `checks/observability/health-check.yaml`
(`tests/test_skill_engine.py::TestDetectModeParity`), applied here to
every remaining legacy check, per
docs/extension-model-unification-plan-2026-07-18.md's Phase 4: "port one
file, write a parity test proving the detect-mode skill produces
identical findings to the YAML check it replaces, verify the parity test
passes, delete the YAML file, commit. Repeat per file." Each class in this
file corresponds to exactly one such commit; the YAML file each class
names in its docstring is already deleted by the time that class's
commit lands (parity was proven *before* deletion, in the same commit).
"""
from __future__ import annotations

from pathlib import Path

from agentit.check_engine import run_checks
from agentit.models import Severity
from agentit.skill_engine import detect_check_definitions, load_skill

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _fires(skill_path: Path, create_mock_repo, files: dict[str, str]):
    """Load *skill_path*, compile its rule, run it against a mock repo
    engineered to fail the rule, and return the single resulting Finding
    -- asserting along the way that it's a real, active detect-mode skill
    whose rule actually compiles (mirrors
    `TestDetectModeParity.test_real_ported_skill_loads_and_compiles`)."""
    skill = load_skill(skill_path)
    assert skill is not None, f"failed to load {skill_path}"
    assert skill.mode == "detect"
    defs = detect_check_definitions([skill])
    assert len(defs) == 1, f"{skill_path} rule did not compile to exactly one CheckDefinition"
    repo = create_mock_repo(files)
    findings = run_checks(defs, repo)
    assert len(findings) == 1, f"{skill_path} expected exactly 1 finding, got {len(findings)}"
    return findings[0]


def _passes(skill_path: Path, create_mock_repo, files: dict[str, str]) -> None:
    """Same setup as `_fires`, but against a mock repo engineered to
    satisfy the rule -- asserts zero findings."""
    skill = load_skill(skill_path)
    assert skill is not None, f"failed to load {skill_path}"
    assert skill.mode == "detect"
    defs = detect_check_definitions([skill])
    assert len(defs) == 1, f"{skill_path} rule did not compile to exactly one CheckDefinition"
    repo = create_mock_repo(files)
    assert run_checks(defs, repo) == []


class TestCiPipelineExistsParity:
    """Ported from `checks/cicd/ci-pipeline.yaml` (deleted in this commit)
    to `skills/cicd/ci-pipeline-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "cicd" / "ci-pipeline-exists.md"

    def test_fires_when_no_gitlab_ci_file(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "pipeline"
        assert finding.severity == Severity.high
        assert finding.description == "No GitLab CI pipeline configuration found"
        assert finding.recommendation == "Create .gitlab-ci.yml or Tekton Pipeline for build/test/scan/deploy"

    def test_passes_when_gitlab_ci_file_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {".gitlab-ci.yml": "stages: [build]\n"})
