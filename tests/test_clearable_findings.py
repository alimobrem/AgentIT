"""Finding-clear remediations: live cluster + source patches + nested migration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentit.analyzers.data_governance import DataGovernanceAnalyzer
from agentit.analyzers.eol import _scan_package_json
from agentit.analyzers.live_evidence import apply_live_cluster_finding_clear
from agentit.models import DimensionScore, Finding, Language, Severity
from agentit.remediation.source_patches import generate_source_patch_for_skill
from agentit.skill_engine import SkillEngine, load_skill
from conftest import make_report


class TestLiveClusterFindingClear:
    def test_drops_quota_and_scaling_when_live(self):
        scores = [
            DimensionScore(
                dimension="infrastructure", score=50, max_score=100,
                findings=[
                    Finding(
                        category="quota", severity=Severity.low,
                        description="No ResourceQuota", recommendation="add",
                    ),
                    Finding(
                        category="eol", severity=Severity.high,
                        description="node 20 eol", recommendation="upgrade",
                    ),
                ],
            ),
            DimensionScore(
                dimension="ha_dr", score=50, max_score=100,
                findings=[
                    Finding(
                        category="scaling", severity=Severity.medium,
                        description="No HPA", recommendation="add",
                    ),
                ],
            ),
        ]
        with (
            patch(
                "agentit.analyzers.live_evidence.live_quota_present",
                return_value=True,
            ),
            patch(
                "agentit.analyzers.live_evidence.live_hpa_present",
                return_value=True,
            ),
        ):
            out = apply_live_cluster_finding_clear(scores, "pinky")
        cats = {f.category for s in out for f in s.findings}
        assert cats == {"eol"}

    def test_discovery_failure_does_not_clear(self):
        scores = [
            DimensionScore(
                dimension="ha_dr", score=50, max_score=100,
                findings=[
                    Finding(
                        category="scaling", severity=Severity.medium,
                        description="No HPA", recommendation="add",
                    ),
                ],
            ),
        ]
        with patch(
            "agentit.analyzers.live_evidence.live_hpa_present",
            return_value=None,
        ), patch(
            "agentit.analyzers.live_evidence.live_quota_present",
            return_value=None,
        ):
            out = apply_live_cluster_finding_clear(scores, "pinky")
        assert [f.category for f in out[0].findings] == ["scaling"]


class TestNestedMigrationDetection:
    def test_nested_alembic_clears_migration_finding(self, tmp_path: Path):
        (tmp_path / "apps" / "api" / "alembic" / "versions").mkdir(parents=True)
        (tmp_path / "apps" / "api" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
        score = DataGovernanceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "migration" not in cats


class TestNodeVersionPinClearsEol:
    def test_node_version_preferred_over_engines(self, tmp_path: Path):
        from datetime import date

        (tmp_path / "package.json").write_text(
            '{"engines": {"node": ">=20"}}', encoding="utf-8",
        )
        (tmp_path / ".node-version").write_text("22\n", encoding="utf-8")
        # Node 22 should not be past EOL as of mid-2026 table — if the table
        # marks it near-EOL that's still a different finding than 20.
        findings = _scan_package_json(tmp_path, date(2026, 7, 22))
        # Either no finding (22 supported) or finding cites .node-version
        if findings:
            assert findings[0].file_path == ".node-version"
            assert "20" not in findings[0].description


class TestSourcePatchSkills:
    def test_containerfile_emits_dockerfile_target(self):
        skill = load_skill(Path("skills/security/containerfile.md"))
        assert skill is not None
        assert skill.delivery == "source"
        report = make_report(
            repo_name="pinky",
            languages=[Language(name="typescript", file_count=10, percentage=80.0)],
            scores=[
                DimensionScore(
                    dimension="security", score=50, max_score=100,
                    findings=[
                        Finding(
                            category="container", severity=Severity.medium,
                            description="Using :latest tag in base image in Dockerfile",
                            recommendation="Pin base image",
                            file_path="Dockerfile",
                        ),
                    ],
                ),
            ],
        )
        files = generate_source_patch_for_skill(skill, report, "pinky")
        assert len(files) == 1
        assert files[0].target_path == "Dockerfile"
        assert ":latest" not in files[0].content
        assert "USER 1001" in files[0].content

    def test_eol_upgrade_emits_node_version(self):
        skill = load_skill(Path("skills/infrastructure/eol-upgrade.md"))
        assert skill is not None
        report = make_report(repo_name="pinky")
        report.scores = [
            DimensionScore(
                dimension="infrastructure", score=50, max_score=100,
                findings=[
                    Finding(
                        category="eol", severity=Severity.high,
                        description="node 20 is past end-of-life (EOL 2026-04-30)",
                        recommendation="Upgrade node",
                        file_path="package.json",
                    ),
                ],
            ),
        ]
        files = generate_source_patch_for_skill(skill, report, "pinky")
        assert len(files) == 1
        assert files[0].target_path == ".node-version"
        assert files[0].content.strip() == "22"

    def test_app_audit_logging_emits_audit_module(self):
        skill = load_skill(Path("skills/compliance/app-audit-logging.md"))
        assert skill is not None
        report = make_report(
            repo_name="pinky",
            languages=[Language(name="typescript", file_count=10, percentage=80.0)],
            scores=[
                DimensionScore(
                    dimension="compliance", score=50, max_score=100,
                    findings=[
                        Finding(
                            category="audit", severity=Severity.high,
                            description="No audit logging implementation detected",
                            recommendation="Add audit logging",
                        ),
                    ],
                ),
            ],
        )
        files = generate_source_patch_for_skill(skill, report, "pinky")
        assert len(files) == 1
        assert files[0].target_path == "audit.ts"
        assert "auditLog" in files[0].content

    def test_skill_engine_source_delivery_sets_target_path(self):
        engine = SkillEngine(Path("skills"), platform=None)
        skill = engine.skill_for_category("container")
        assert skill is not None
        assert skill.delivery == "source"
        report = make_report(repo_name="demo")
        report.scores = [
            DimensionScore(
                dimension="security", score=40, max_score=100,
                findings=[
                    Finding(
                        category="container", severity=Severity.medium,
                        description="No Dockerfile or Containerfile found",
                        recommendation="Create Containerfile",
                    ),
                ],
            ),
        ]
        files = engine.generate(skill, report, llm_client=None)
        assert files
        assert files[0].target_path in ("Dockerfile", "Containerfile")
