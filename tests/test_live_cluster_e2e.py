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
from agentit.agents.cicd import CICDAgent
from agentit.agents.compliance import ComplianceAgent
from agentit.agents.hardening import HardeningAgent
from agentit.agents.observability import ObservabilityAgent
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


# ── Phase 2: Onboarding (manifest generation) ──────────────────────


@pytest.mark.live_cluster
class TestOnboarding:
    @pytest.fixture(autouse=True)
    def _setup(self, assessment_report, tmp_path):
        self.report = assessment_report
        self.output_dir = tmp_path / "onboard-output"
        self.output_dir.mkdir()

    def test_hardening_generates_manifests(self):
        result = HardeningAgent(self.report, self.output_dir / "security").run()
        assert len(result.files) > 0
        for f in result.files:
            if f.path.endswith(".yaml"):
                errors = validate_manifest(f.content)
                assert not errors, f"{f.path}: {errors}"

    def test_observability_generates_manifests(self):
        result = ObservabilityAgent(self.report, self.output_dir / "observability").run()
        assert len(result.files) > 0
        for f in result.files:
            if f.path.endswith(".yaml"):
                errors = validate_manifest(f.content)
                assert not errors, f"{f.path}: {errors}"

    def test_cicd_generates_pipeline(self):
        result = CICDAgent(self.report, self.output_dir / "cicd").run()
        pipeline_files = [f for f in result.files if "pipeline" in f.path.lower()]
        assert len(pipeline_files) > 0

    def test_pipeline_includes_scan_and_sbom_steps(self):
        result = CICDAgent(self.report, self.output_dir / "cicd").run()
        pipeline_file = next((f for f in result.files if f.path.endswith("pipeline.yaml")), None)
        assert pipeline_file is not None
        assert "image-scan" in pipeline_file.content
        assert "sbom-generate" in pipeline_file.content

    def test_compliance_generates_sbom_task(self):
        result = ComplianceAgent(self.report, self.output_dir / "compliance").run()
        sbom_tasks = [f for f in result.files if "sbom" in f.path]
        assert len(sbom_tasks) > 0
        assert sbom_tasks[0].path.endswith(".yaml")
        assert "kind: Task" in sbom_tasks[0].content

    def test_hardening_generates_scan_task(self):
        result = HardeningAgent(self.report, self.output_dir / "security").run()
        scan_tasks = [f for f in result.files if "scan" in f.path]
        assert len(scan_tasks) > 0
        assert "kind: Task" in scan_tasks[0].content

    def test_observability_generates_grafana_configmap(self):
        result = ObservabilityAgent(self.report, self.output_dir / "observability").run()
        cm_files = [f for f in result.files if "grafana" in f.path and f.path.endswith(".yaml")]
        assert len(cm_files) > 0
        assert "kind: ConfigMap" in cm_files[0].content
        assert "grafana_dashboard" in cm_files[0].content


# ── Phase 2b: Pipeline Wiring Verification ─────────────────────────


@pytest.mark.live_cluster
class TestPipelineWiring:
    """Verify that generated Tekton Tasks are actually referenced by the pipeline.

    This catches the 'dead file' problem: an agent generates a Task YAML but
    the CI/CD pipeline never calls it, so it sits unused in the repo.
    """

    @pytest.fixture(autouse=True)
    def _run_agents(self, assessment_report, tmp_path):
        self.report = assessment_report
        out = tmp_path / "wiring-test"
        self.hardening = HardeningAgent(self.report, out / "security").run()
        self.compliance = ComplianceAgent(self.report, out / "compliance").run()
        self.cicd = CICDAgent(self.report, out / "cicd").run()
        self.observability = ObservabilityAgent(self.report, out / "observability").run()

    def test_pipeline_references_image_scan_task(self):
        """The pipeline must include an image-scan step that refs the Task the hardening agent generates."""
        scan_tasks = [f for f in self.hardening.files if "scan" in f.path and f.path.endswith(".yaml")]
        if not scan_tasks:
            pytest.skip("Hardening agent didn't generate scan task (no scanning findings)")

        import yaml
        scan_task_name = yaml.safe_load(scan_tasks[0].content)["metadata"]["name"]

        pipeline_file = next((f for f in self.cicd.files if "pipeline.yaml" in f.path), None)
        assert pipeline_file is not None, "CI/CD agent didn't generate a pipeline"

        pipeline = yaml.safe_load(pipeline_file.content)
        task_refs = [t.get("taskRef", {}).get("name") for t in pipeline["spec"]["tasks"]]
        assert scan_task_name in task_refs, (
            f"Pipeline does not reference scan task '{scan_task_name}'. "
            f"Pipeline taskRefs: {task_refs}"
        )

    def test_pipeline_references_sbom_task(self):
        """The pipeline must include an sbom-generate step that refs the Task the compliance agent generates."""
        sbom_tasks = [f for f in self.compliance.files if "sbom" in f.path and f.path.endswith(".yaml")]
        if not sbom_tasks:
            pytest.skip("Compliance agent didn't generate SBOM task (no SBOM findings)")

        import yaml
        sbom_task_name = yaml.safe_load(sbom_tasks[0].content)["metadata"]["name"]

        pipeline_file = next((f for f in self.cicd.files if "pipeline.yaml" in f.path), None)
        assert pipeline_file is not None

        pipeline = yaml.safe_load(pipeline_file.content)
        task_refs = [t.get("taskRef", {}).get("name") for t in pipeline["spec"]["tasks"]]
        assert sbom_task_name in task_refs, (
            f"Pipeline does not reference SBOM task '{sbom_task_name}'. "
            f"Pipeline taskRefs: {task_refs}"
        )

    def test_deploy_runs_after_scan_and_sbom(self):
        """Deploy step must depend on both scan and SBOM (runAfter)."""
        pipeline_file = next((f for f in self.cicd.files if "pipeline.yaml" in f.path), None)
        assert pipeline_file is not None

        import yaml
        pipeline = yaml.safe_load(pipeline_file.content)
        deploy_task = next((t for t in pipeline["spec"]["tasks"] if t["name"] == "deploy"), None)
        assert deploy_task is not None, "No deploy task in pipeline"

        run_after = deploy_task.get("runAfter", [])
        assert "image-scan" in run_after, f"Deploy doesn't wait for image-scan: runAfter={run_after}"
        assert "sbom-generate" in run_after, f"Deploy doesn't wait for sbom-generate: runAfter={run_after}"

    def test_grafana_dashboard_is_configmap_not_raw_json(self):
        """Dashboard must be a ConfigMap (auto-imported by Grafana sidecar), not a raw JSON file."""
        grafana_files = [f for f in self.observability.files if "grafana" in f.path]
        assert len(grafana_files) > 0

        for gf in grafana_files:
            assert gf.path.endswith(".yaml"), f"Grafana file '{gf.path}' should be YAML ConfigMap, not raw JSON"
            assert "kind: ConfigMap" in gf.content, f"Grafana file '{gf.path}' should be a ConfigMap"
            assert "grafana_dashboard" in gf.content, f"Grafana ConfigMap missing grafana_dashboard label"

    def test_no_shell_scripts_in_generated_files(self):
        """No agent should generate .sh files — use Tekton Tasks instead."""
        all_files = (
            list(self.hardening.files) +
            list(self.compliance.files) +
            list(self.cicd.files) +
            list(self.observability.files)
        )
        shell_scripts = [f for f in all_files if f.path.endswith(".sh")]
        assert len(shell_scripts) == 0, (
            f"Found shell scripts that should be Tekton Tasks: "
            f"{[f.path for f in shell_scripts]}"
        )

    def test_all_yaml_files_are_valid_manifests(self):
        """Every .yaml file should pass manifest validation."""
        all_files = (
            list(self.hardening.files) +
            list(self.compliance.files) +
            list(self.cicd.files) +
            list(self.observability.files)
        )
        for f in all_files:
            if f.path.endswith((".yaml", ".yml")):
                errors = validate_manifest(f.content)
                assert not errors, f"{f.path} failed validation: {errors}"


# ── Phase 3: Orchestrator ──────────────────────────────────────────


@pytest.mark.live_cluster
class TestOrchestrator:
    def test_orchestrator_runs_all_agents(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "orch-output",
            store=store,
            assessment_id=aid,
        )
        result = orch.run()
        agent_names = {a.agent_name for a in result.agent_results}
        assert "security" in agent_names
        assert "observability" in agent_names
        assert "cicd" in agent_names
        assert "compliance" in agent_names

    def test_orchestrator_produces_recommendation(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "orch-output2",
            store=store,
            assessment_id=aid,
        )
        result = orch.run()
        assert result.recommendation

    def test_all_manifests_validate(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "orch-output3",
            store=store,
            assessment_id=aid,
        )
        result = orch.run()
        for ar in result.agent_results:
            for f in ar.files_generated:
                full = tmp_path / "orch-output3" / ar.category / f
                if full.exists() and full.suffix in (".yaml", ".yml"):
                    content = full.read_text()
                    errors = validate_manifest(content)
                    assert not errors, f"{ar.category}/{f}: {errors}"


# ── Phase 4: Cluster Apply (dry-run) ──────────────────────────────


@pytest.mark.live_cluster
class TestClusterApply:
    def test_dry_run_applies_without_errors(self, assessment_report, store, tmp_path, test_namespace):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "apply-output",
            store=store,
            assessment_id=aid,
        )
        result = orch.run()

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
        assert retrieved["repo_name"] == assessment_report.repo_name
        assert retrieved["overall_score"] == assessment_report.overall_score

    def test_onboarding_creates_files(self, assessment_report, store, tmp_path):
        aid = store.save(assessment_report)
        orch = FleetOrchestrator(
            report=assessment_report,
            output_dir=tmp_path / "store-test",
            store=store,
            assessment_id=aid,
        )
        result = orch.run()
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

    def test_full_pipeline(self, tmp_path, test_namespace):
        store = AssessmentStore(db_path=str(tmp_path / "e2e.db"))

        report = run_assessment(FIXTURE_DIR, repo_url="file://" + str(FIXTURE_DIR), criticality="high")
        aid = store.save(report)
        assert report.overall_score < 60

        orch = FleetOrchestrator(
            report=report,
            output_dir=tmp_path / "full-pipeline",
            store=store,
            assessment_id=aid,
        )
        result = orch.run()
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
