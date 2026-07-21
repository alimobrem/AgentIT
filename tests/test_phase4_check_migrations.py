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


class TestDockerfileExistsParity:
    """Ported from `checks/cicd/dockerfile.yaml` (deleted in this commit)
    to `skills/cicd/dockerfile-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "cicd" / "dockerfile-exists.md"

    def test_fires_when_no_dockerfile(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "container"
        assert finding.severity == Severity.high
        assert finding.description == "No Dockerfile found for container builds"
        assert finding.recommendation == "Create multi-stage Dockerfile with UBI base image"

    def test_passes_when_dockerfile_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {"Dockerfile": "FROM ubi9\n"})


class TestArgocdApplicationExistsParity:
    """Ported from `checks/cicd/gitops.yaml` (deleted in this commit) to
    `skills/cicd/argocd-application-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "cicd" / "argocd-application-exists.md"

    def test_fires_when_no_argoproj_reference(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "gitops"
        assert finding.severity == Severity.medium
        assert finding.description == "No GitOps configuration (Argo CD) detected"
        assert finding.recommendation == "Create Argo CD Application for GitOps delivery"

    def test_passes_when_argoproj_reference_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {
            "argocd/application.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: Application\n",
        })


class TestAdmissionPoliciesExistParity:
    """Ported from `checks/compliance/admission-policies.yaml` (deleted in
    this commit) to `skills/compliance/admission-policies-exist.md`."""

    SKILL_PATH = SKILLS_DIR / "compliance" / "admission-policies-exist.md"

    def test_fires_when_no_policy_manifest(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {
            "deploy/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n",
        })
        assert finding.category == "policy"
        assert finding.severity == Severity.medium
        assert finding.description == "No admission policies (Kyverno/OPA/Gatekeeper) found"
        assert finding.recommendation == "Create Kyverno policies for resource limits, labels, approved base images"

    def test_passes_when_namespaced_policy_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {
            "policies/require-labels.yaml": "apiVersion: kyverno.io/v1\nkind: Policy\n",
        })

    def test_does_not_match_clusterpolicy_only(self, create_mock_repo) -> None:
        """Deliberately narrow, per this file's own docstring -- a
        cluster-scoped ClusterPolicy alone must not satisfy the rule,
        matching the deleted YAML's exact (not broadened) scope."""
        finding = _fires(self.SKILL_PATH, create_mock_repo, {
            "policies/require-labels.yaml": "apiVersion: kyverno.io/v1\nkind: ClusterPolicy\n",
        })
        assert finding.category == "policy"


class TestLicenseFileExistsParity:
    """Ported from `checks/compliance/license.yaml` (deleted in this
    commit) to `skills/compliance/license-file-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "compliance" / "license-file-exists.md"

    def test_fires_when_no_license_file(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "license"
        assert finding.severity == Severity.high
        assert finding.description == "No LICENSE file found"
        assert finding.recommendation == "Add a LICENSE file (Apache 2.0 recommended for enterprise open source)"

    def test_passes_when_license_file_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {"LICENSE": "Apache License 2.0\n"})


class TestSbomExistsParity:
    """Ported from `checks/compliance/sbom.yaml` (deleted in this commit)
    to `skills/compliance/sbom-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "compliance" / "sbom-exists.md"

    def test_fires_when_no_sbom_file(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "sbom"
        assert finding.severity == Severity.high
        assert finding.description == "No SBOM (Software Bill of Materials) found"
        assert finding.recommendation == "Generate SBOM using Syft, store in ODF"

    def test_passes_when_sbom_file_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {"sbom.json": "{}\n"})


class TestBackupConfigExistsParity:
    """Ported from `checks/data_governance/backup-config.yaml` (deleted in
    this commit) to `skills/data_governance/backup-config-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "data_governance" / "backup-config-exists.md"

    def test_fires_when_no_backup_reference(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "backup"
        assert finding.severity == Severity.high
        assert finding.description == "No backup configuration detected"
        assert finding.recommendation == "Configure Crunchy PostgreSQL backup schedule or add backup CronJob"

    def test_passes_when_backup_reference_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {
            "backup-cronjob.yaml": "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: backup\n",
        })


class TestRetentionPolicyExistsParity:
    """Ported from `checks/data_governance/retention-policy.yaml` (deleted
    in this commit) to `skills/data_governance/retention-policy-exists.md`."""

    SKILL_PATH = SKILLS_DIR / "data_governance" / "retention-policy-exists.md"

    def test_fires_when_no_retention_reference(self, create_mock_repo) -> None:
        finding = _fires(self.SKILL_PATH, create_mock_repo, {"main.py": "print('hi')\n"})
        assert finding.category == "retention"
        assert finding.severity == Severity.medium
        assert finding.description == "No data retention policy detected"
        assert finding.recommendation == "Define data retention policies for compliance (GDPR, SOC 2)"

    def test_passes_when_retention_reference_present(self, create_mock_repo) -> None:
        _passes(self.SKILL_PATH, create_mock_repo, {
            "docs/data-retention.md": "# Data Retention Policy\n\nretention period: 90 days.\n",
        })
