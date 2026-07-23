"""Detect + contract coverage for image_signing / cosign-sign-task."""
from __future__ import annotations

from pathlib import Path

from agentit.check_engine import run_checks
from agentit.models import Severity
from agentit.portal.quality_prs import filter_files_to_open_findings
from agentit.remediation.registry import FIX_REGISTRY, contract_for, lookup
from agentit.skill_engine import SkillEngine, detect_check_definitions, load_skill

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
DETECT_PATH = SKILLS_DIR / "security" / "image-signing-exists.md"
REMEDIATE_PATH = SKILLS_DIR / "security" / "cosign-sign-task.md"


class TestImageSigningDetect:
    def test_fires_when_no_cosign(self, create_mock_repo) -> None:
        skill = load_skill(DETECT_PATH)
        assert skill is not None
        assert skill.mode == "detect"
        defs = detect_check_definitions([skill])
        assert len(defs) == 1
        repo = create_mock_repo({
            ".gitlab-ci.yml": "stages: [build]\nbuild:\n  script: echo hi\n",
            "deploy/pipeline.yaml": "apiVersion: tekton.dev/v1\nkind: Pipeline\n",
        })
        findings = run_checks(defs, repo)
        assert len(findings) == 1
        assert findings[0].category == "image_signing"
        assert findings[0].severity == Severity.high
        assert "cosign" in findings[0].description.lower() or "signing" in findings[0].description.lower()

    def test_passes_when_cosign_present(self, create_mock_repo) -> None:
        skill = load_skill(DETECT_PATH)
        defs = detect_check_definitions([skill])
        repo = create_mock_repo({
            "tekton/cosign-sign-task.yaml": (
                "apiVersion: tekton.dev/v1\n"
                "kind: Task\n"
                "spec:\n  steps:\n"
                "  - script: cosign sign --yes $(params.IMAGE)\n"
            ),
        })
        assert run_checks(defs, repo) == []

    def test_slsa_prose_alone_does_not_pass(self, create_mock_repo) -> None:
        skill = load_skill(DETECT_PATH)
        defs = detect_check_definitions([skill])
        repo = create_mock_repo({
            "README.md": "We aim for SLSA Level 3 hermetic Konflux builds.\n",
        })
        findings = run_checks(defs, repo)
        assert len(findings) == 1


class TestImageSigningContract:
    def test_fix_registry_maps_to_cosign_sign_task(self) -> None:
        assert lookup("image_signing") == ("security", "cosign-sign-task")
        assert FIX_REGISTRY["image_signing"] == ("security", "cosign-sign-task")

    def test_contract_cluster_delivery_and_refuse_companions(self) -> None:
        c = contract_for("image_signing")
        assert c is not None
        assert c.auto_pr is True
        assert c.delivery == "cluster"
        assert c.evidence_kind == "cosign_sign_task"
        assert "image-scan-task" in c.refuse_companions
        assert "sbom-task" in c.refuse_companions
        assert "compliance-evidence" in c.refuse_companions

    def test_skill_for_category_resolves(self) -> None:
        engine = SkillEngine(SKILLS_DIR, platform=None)
        skill = engine.skill_for_category("image_signing")
        assert skill is not None
        assert skill.name == "cosign-sign-task"

    def test_remediate_skill_loads_with_cluster_delivery(self) -> None:
        skill = load_skill(REMEDIATE_PATH)
        assert skill is not None
        assert skill.mode == "template"
        assert (skill.delivery or "cluster") == "cluster"
        assert "cosign sign" in skill.body.lower()

    def test_filter_refuses_scan_companion(self) -> None:
        findings = [("image_signing", "No cosign/Sigstore image signing detected")]
        files = [
            {
                "category": "skills",
                "path": "pinky-cosign-sign-task.yaml",
                "content": (
                    "apiVersion: tekton.dev/v1\nkind: Task\n"
                    "spec:\n  steps:\n  - script: cosign sign --yes img\n"
                ),
                "skill_name": "cosign-sign-task",
            },
            {
                "category": "skills",
                "path": "pinky-image-scan-task.yaml",
                "content": "apiVersion: tekton.dev/v1\nkind: Task\n",
                "skill_name": "image-scan-task",
            },
        ]
        kept, drops = filter_files_to_open_findings(files, findings)
        assert len(kept) == 1
        assert kept[0]["skill_name"] == "cosign-sign-task"
        assert len(drops) == 1
