"""End-to-end tests against a live OpenShift cluster.

Run with:
    pytest tests/test_live_cluster_e2e.py --live-cluster -v -s

Prerequisites:
    - Active `oc login` session
    - Namespace 'agentit-e2e-test' will be created and cleaned up
    - The sample-app fixture at tests/fixtures/sample-app/ is used as the repo
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from agentit.agents.base import validate_manifest
from agentit.agents.orchestrator import FleetOrchestrator
from agentit.portal.cluster_apply import apply_manifests_to_cluster
from agentit.portal.store import AssessmentStore
from agentit.runner import run_assessment

logger = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample-app"
TEST_NAMESPACE = "agentit-e2e-test"


def _run_oc(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["oc", *args], capture_output=True, text=True, timeout=30, check=check,
    )


def _cluster_available() -> bool:
    try:
        result = _run_oc("whoami", check=False)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def cluster_check():
    if not _cluster_available():
        pytest.skip("No active oc login session")


@pytest.fixture(scope="module")
def test_namespace(cluster_check):
    _run_oc("new-project", TEST_NAMESPACE, check=False)
    _run_oc("project", TEST_NAMESPACE)
    yield TEST_NAMESPACE
    _run_oc("delete", "project", TEST_NAMESPACE, "--wait=false", check=False)


@pytest.fixture(scope="module")
def assessment_report():
    return run_assessment(FIXTURE_DIR, repo_url="file://" + str(FIXTURE_DIR), criticality="high")


@pytest.fixture(scope="module")
def store():
    return AssessmentStore(db_path=":memory:")


# ── Phase 1: Assessment ────────────────────────────────────────────


@pytest.mark.live_cluster
class TestAssessment:
    def test_detects_python_stack(self, assessment_report):
        lang_names = [l.name for l in assessment_report.stack.languages]
        assert "python" in lang_names

    def test_detects_flask_framework(self, assessment_report):
        fw_names = [f.name for f in assessment_report.stack.frameworks]
        assert "flask" in fw_names

    def test_produces_seven_dimension_scores(self, assessment_report):
        assert len(assessment_report.scores) == 7

    def test_finds_security_issues(self, assessment_report):
        sec = next(s for s in assessment_report.scores if s.dimension == "security")
        assert sec.score < 80
        assert len(sec.findings) > 0

    def test_overall_score_is_low(self, assessment_report):
        assert assessment_report.overall_score < 60


# ── Phase 2: Onboarding (manifest generation via skills) ───────────
#
# HardeningAgent/ObservabilityAgent/CICDAgent/ComplianceAgent were removed
# once skills gained full template-fallback parity for their domains (see
# docs/agent-removal-readiness.md) -- the equivalent live-cluster-flavored
# coverage (real manifests, valid YAML, Tekton pipeline wiring) for their
# skill replacements is exercised hermetically in
# tests/test_skill_agent_parity.py rather than re-added here; porting the
# specific pipeline-wiring assertions below (image-scan/sbom task cross-
# references between independently-matched skills) would need real design
# work the skill engine doesn't support today (skills don't know about each
# other's output), so it's left as a documented gap rather than force-fit.


# ── Phase 3: Orchestrator ──────────────────────────────────────────


@pytest.mark.live_cluster
class TestOrchestrator:
    async def test_orchestrator_runs_all_agents(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "orch-output",
            store=AsyncSQLiteStore.wrap(store),
            assessment_id=aid,
        )
        result = await orch.run()
        agent_names = {a.agent_name for a in result.agent_results}
        # security/observability/cicd/compliance are now skill-only domains
        # (see docs/agent-removal-readiness.md) -- skills run
        # unconditionally and report under the shared "skills" name.
        assert "skills" in agent_names

    async def test_orchestrator_produces_recommendation(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "orch-output2",
            store=AsyncSQLiteStore.wrap(store),
            assessment_id=aid,
        )
        result = await orch.run()
        assert result.recommendation

    async def test_all_manifests_validate(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "orch-output3",
            store=AsyncSQLiteStore.wrap(store),
            assessment_id=aid,
        )
        result = await orch.run()
        for ar in result.agent_results:
            for f in ar.files_generated:
                full = tmp_path / "orch-output3" / ar.category / f
                if full.exists() and full.suffix in (".yaml", ".yml") and "audit-policy" not in f:
                    content = full.read_text()
                    errors = validate_manifest(content)
                    assert not errors, f"{ar.category}/{f}: {errors}"


# ── Phase 4: Cluster Apply (dry-run) ──────────────────────────────


@pytest.mark.live_cluster
class TestClusterApply:
    async def test_dry_run_applies_without_errors(self, assessment_report, store, tmp_path, test_namespace):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "apply-output",
            store=AsyncSQLiteStore.wrap(store),
            assessment_id=aid,
        )
        result = await orch.run()

        files = []
        for ar in result.agent_results:
            if not ar.success:
                continue
            out_dir = tmp_path / "apply-output" / ar.category
            for fname in ar.files_generated:
                full = out_dir / fname
                if full.exists() and full.suffix in (".yaml", ".yml"):
                    files.append({
                        "category": ar.category,
                        "path": fname,
                        "content": full.read_text(),
                        "description": fname,
                    })

        apply_result = apply_manifests_to_cluster(files, test_namespace, dry_run=True)
        logger.info("Dry-run: %d applied, %d skipped, %d errors",
                     len(apply_result["applied"]), len(apply_result["skipped"]),
                     len(apply_result["errors"]))

        for err in apply_result["errors"]:
            if "resource mapping not found" in err:
                logger.warning("CRD missing (expected in test): %s", err.split(":")[0])
            else:
                logger.error("Unexpected apply error: %s", err)

        crd_errors = [e for e in apply_result["errors"] if "resource mapping not found" in e]
        real_errors = [e for e in apply_result["errors"] if "resource mapping not found" not in e]
        assert len(real_errors) == 0, f"Non-CRD errors during dry-run: {real_errors}"


# ── Phase 5: Store + Portal Integration ────────────────────────────


@pytest.mark.live_cluster
class TestStoreIntegration:
    def test_assessment_persists_and_retrieves(self, assessment_report, store):
        aid = store.save(assessment_report)
        retrieved = store.get(aid)
        assert retrieved is not None
        assert retrieved.repo_name == assessment_report.repo_name
        assert retrieved.overall_score == assessment_report.overall_score

    async def test_onboarding_creates_files(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "store-test",
            store=AsyncSQLiteStore.wrap(store),
            assessment_id=aid,
        )
        result = await orch.run()
        total_files = sum(len(ar.files_generated) for ar in result.agent_results)
        assert total_files > 0

    def test_fleet_data_includes_assessment(self, assessment_report, store):
        store.save(assessment_report)
        fleet = store.get_fleet_data()
        repo_names = [a["repo_name"] for a in fleet]
        assert assessment_report.repo_name in repo_names


# ── Phase 6: Full Pipeline Smoke Test ──────────────────────────────


@pytest.mark.live_cluster
class TestFullPipeline:
    """Runs the full assess → onboard → dry-run apply pipeline end-to-end."""

    async def test_full_pipeline(self, tmp_path, test_namespace):
        store = AssessmentStore(db_path=str(tmp_path / "e2e.db"))

        report = run_assessment(FIXTURE_DIR, repo_url="file://" + str(FIXTURE_DIR), criticality="high")
        aid = store.save(report)
        assert report.overall_score < 60

        orch = FleetOrchestrator(
            report=report,
            output_dir=tmp_path / "full-pipeline",
            store=AsyncSQLiteStore.wrap(store),
            assessment_id=aid,
        )
        result = await orch.run()
        assert any(ar.success for ar in result.agent_results)

        files = []
        for ar in result.agent_results:
            if not ar.success:
                continue
            out_dir = tmp_path / "full-pipeline" / ar.category
            for fname in ar.files_generated:
                full = out_dir / fname
                if full.exists() and full.suffix in (".yaml", ".yml"):
                    files.append({
                        "category": ar.category,
                        "path": fname,
                        "content": full.read_text(),
                        "description": fname,
                    })

        assert len(files) > 0, "No YAML manifests generated"

        apply_result = apply_manifests_to_cluster(files, test_namespace, dry_run=True)
        real_errors = [
            e for e in apply_result["errors"]
            if "resource mapping not found" not in e
        ]
        assert len(real_errors) == 0, f"Pipeline dry-run errors: {real_errors}"

        pipeline_files = [f for f in files if "pipeline" in f["path"].lower()]
        assert len(pipeline_files) > 0, "No Tekton pipeline generated"

        scan_files = [f for f in files if "scan" in f["path"].lower()]
        assert len(scan_files) > 0, "No image scan task generated"

        sbom_files = [f for f in files if "sbom" in f["path"].lower()]
        assert len(sbom_files) > 0, "No SBOM task generated"

        grafana_files = [f for f in files if "grafana" in f["path"].lower()]
        assert len(grafana_files) > 0, "No Grafana dashboard ConfigMap generated"
        assert any("ConfigMap" in f["content"] for f in grafana_files)

        logger.info(
            "Full pipeline: score=%d, agents=%d, manifests=%d, applied=%d, skipped=%d, crd_missing=%d",
            report.overall_score,
            len(result.agent_results),
            len(files),
            len(apply_result["applied"]),
            len(apply_result["skipped"]),
            len([e for e in apply_result["errors"] if "resource mapping not found" in e]),
        )
