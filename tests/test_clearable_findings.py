"""Finding-clear remediations: live cluster + source patches + nested migration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentit.analyzers.data_governance import DataGovernanceAnalyzer
from agentit.analyzers.eol import _scan_package_json
from agentit.analyzers.infrastructure import InfrastructureAnalyzer
from agentit.analyzers.live_evidence import (
    apply_live_cluster_finding_clear,
    live_hpa_present,
)
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
        ), patch(
            "agentit.analyzers.live_evidence.live_health_probes_present",
            return_value=None,
        ):
            out = apply_live_cluster_finding_clear(scores, "pinky")
        assert [f.category for f in out[0].findings] == ["scaling"]

    def test_broken_hpa_scale_target_does_not_clear(self):
        """HPA present but scaleTargetRef missing must not clear scaling."""
        broken = [{
            "metadata": {"name": "pinky-hpa"},
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "pinky",
                },
            },
        }]
        with (
            patch("agentit.kube.list_custom_resources", return_value=broken),
            patch("agentit.kube.apps_v1") as apps_v1,
        ):
            apps_v1.return_value.read_namespaced_deployment.side_effect = (
                Exception('deployments.apps "pinky" not found')
            )
            assert live_hpa_present("pinky") is False

    def test_drops_health_when_live_workloads_have_probes(self):
        """ha_dr.py's `health` finding is source-only (iter_yaml_files on the
        app repo) and never learns a fix landed elsewhere (a GitOps-synced
        Deployment edit, or health-probes-policy.md's Kyverno mutate policy)
        -- the same disconnect quota/scaling already had."""
        scores = [
            DimensionScore(
                dimension="ha_dr", score=50, max_score=100,
                findings=[
                    Finding(
                        category="health", severity=Severity.high,
                        description="No liveness or readiness probes defined",
                        recommendation="Add probes",
                    ),
                    Finding(
                        category="availability", severity=Severity.high,
                        description="Single replica", recommendation="scale",
                    ),
                ],
            ),
        ]
        with (
            patch("agentit.analyzers.live_evidence.live_quota_present", return_value=None),
            patch("agentit.analyzers.live_evidence.live_hpa_present", return_value=None),
            patch("agentit.analyzers.live_evidence.live_health_probes_present", return_value=True),
        ):
            out = apply_live_cluster_finding_clear(scores, "pinky")
        cats = {f.category for s in out for f in s.findings}
        assert cats == {"availability"}

    def test_health_not_cleared_when_a_live_container_is_missing_a_probe(self):
        scores = [
            DimensionScore(
                dimension="ha_dr", score=50, max_score=100,
                findings=[
                    Finding(
                        category="health", severity=Severity.high,
                        description="No liveness or readiness probes defined",
                        recommendation="Add probes",
                    ),
                ],
            ),
        ]
        with (
            patch("agentit.analyzers.live_evidence.live_quota_present", return_value=None),
            patch("agentit.analyzers.live_evidence.live_hpa_present", return_value=None),
            patch("agentit.analyzers.live_evidence.live_health_probes_present", return_value=False),
        ):
            out = apply_live_cluster_finding_clear(scores, "pinky")
        assert [f.category for f in out[0].findings] == ["health"]


class TestLiveHealthProbesPresent:
    """Unit coverage for live_evidence.live_health_probes_present()'s own
    tri-state discovery logic (True/False/None), independent of the
    finding-clear wiring above."""

    @staticmethod
    def _make_container(has_liveness: bool, has_readiness: bool) -> MagicMock:
        c = MagicMock()
        c.liveness_probe = MagicMock() if has_liveness else None
        c.readiness_probe = MagicMock() if has_readiness else None
        return c

    def _make_deployment(self, containers: list) -> MagicMock:
        dep = MagicMock()
        dep.spec.template.spec.containers = containers
        return dep

    def test_true_when_every_container_has_both_probes(self):
        from agentit.analyzers.live_evidence import live_health_probes_present

        with (
            patch("agentit.kube.apps_v1") as mock_apps,
            patch("agentit.kube.list_custom_resources", return_value=[]),
        ):
            mock_apps.return_value.list_namespaced_deployment.return_value.items = [
                self._make_deployment([self._make_container(True, True)]),
            ]
            assert live_health_probes_present("pinky") is True

    def test_false_when_a_container_is_missing_readiness_probe(self):
        from agentit.analyzers.live_evidence import live_health_probes_present

        with (
            patch("agentit.kube.apps_v1") as mock_apps,
            patch("agentit.kube.list_custom_resources", return_value=[]),
        ):
            mock_apps.return_value.list_namespaced_deployment.return_value.items = [
                self._make_deployment([self._make_container(True, False)]),
            ]
            assert live_health_probes_present("pinky") is False

    def test_none_when_no_live_workloads_found(self):
        from agentit.analyzers.live_evidence import live_health_probes_present

        with (
            patch("agentit.kube.apps_v1") as mock_apps,
            patch("agentit.kube.list_custom_resources", return_value=[]),
        ):
            mock_apps.return_value.list_namespaced_deployment.return_value.items = []
            assert live_health_probes_present("pinky") is None

    def test_none_on_discovery_failure(self):
        from agentit.analyzers.live_evidence import live_health_probes_present

        with patch("agentit.kube.apps_v1", side_effect=RuntimeError("api down")):
            assert live_health_probes_present("pinky") is None

    def test_none_for_empty_namespace(self):
        from agentit.analyzers.live_evidence import live_health_probes_present

        assert live_health_probes_present("") is None

    def test_true_via_rollout_when_deployments_empty_but_rollout_has_probes(self):
        """A live Rollout (dict-shaped custom resource, camelCase keys) with
        probes on every container also counts as real evidence -- not just
        typed Deployment objects."""
        from agentit.analyzers.live_evidence import live_health_probes_present

        rollout = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "pinky", "livenessProbe": {"tcpSocket": {"port": "http"}},
                             "readinessProbe": {"tcpSocket": {"port": "http"}}},
                        ],
                    },
                },
            },
        }
        with (
            patch("agentit.kube.apps_v1") as mock_apps,
            patch("agentit.kube.list_custom_resources", return_value=[rollout]),
        ):
            mock_apps.return_value.list_namespaced_deployment.return_value.items = []
            assert live_health_probes_present("pinky") is True


class TestNestedMigrationDetection:
    def test_nested_alembic_clears_migration_finding(self, tmp_path: Path):
        (tmp_path / "apps" / "api" / "alembic" / "versions").mkdir(parents=True)
        (tmp_path / "apps" / "api" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
        score = DataGovernanceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "migration" not in cats

    def test_hand_rolled_schema_sql_clears_migration_finding(self, tmp_path: Path):
        """AgentIT-style store DDL (ADR 0002) must pass — no stub Alembic PR."""
        store = tmp_path / "src" / "app" / "store"
        store.mkdir(parents=True)
        (store / "_shared.py").write_text(
            'SCHEMA_SQL = """\n'
            "CREATE TABLE IF NOT EXISTS assessments (id TEXT PRIMARY KEY);\n"
            "CREATE TABLE IF NOT EXISTS apps (repo_url TEXT PRIMARY KEY);\n"
            "-- Additive, idempotent column (same no-migration-framework convention)\n"
            "ALTER TABLE apps ADD COLUMN IF NOT EXISTS assessment_cadence TEXT;\n"
            '"""\n',
            encoding="utf-8",
        )
        score = DataGovernanceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "migration" not in cats

    def test_tests_dir_create_table_does_not_count_as_migration(self, tmp_path: Path):
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_schema.py").write_text(
            'SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS t (id INT)"\n'
            'x = "CREATE TABLE IF NOT EXISTS u (id INT)"\n',
            encoding="utf-8",
        )
        score = DataGovernanceAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "migration" in cats


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

    def test_db_migration_emits_revision_not_theater_stub(self):
        from agentit.remediation.clear_evidence import verify_migration_tooling

        skill = load_skill(Path("skills/data_governance/db-migration-tooling.md"))
        assert skill is not None
        report = make_report(
            repo_name="pinky",
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            scores=[
                DimensionScore(
                    dimension="data_governance", score=50, max_score=100,
                    findings=[
                        Finding(
                            category="migration", severity=Severity.medium,
                            description="No database migration tooling detected",
                            recommendation="Add Alembic",
                        ),
                    ],
                ),
            ],
        )
        files = generate_source_patch_for_skill(skill, report, "pinky")
        paths = {f.target_path for f in files}
        assert "alembic.ini" in paths
        assert "alembic/versions/0001_baseline.py" in paths
        assert any("DATABASE_URL" in f.content for f in files if f.target_path == "alembic/env.py")
        staged = [
            {"target_path": f.target_path, "content": f.content, "skill_name": skill.name}
            for f in files
        ]
        ok, reason = verify_migration_tooling(staged)
        assert ok, reason

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

    def test_helm_chart_skill_is_source_delivery_llm_mode(self):
        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        assert skill is not None
        assert skill.delivery == "source"
        assert skill.mode == "llm"

    def test_helm_chart_template_fallback_emits_chart_and_manifests(self):
        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        assert skill is not None
        report = make_report(
            repo_name="pinky",
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            scores=[
                DimensionScore(
                    dimension="infrastructure", score=40, max_score=100,
                    findings=[
                        Finding(
                            category="iac", severity=Severity.high,
                            description="No IaC tooling detected (no Helm chart, Kustomize, or Terraform)",
                            recommendation="Generate Helm chart with values.yaml and environment overlays",
                        ),
                        Finding(
                            category="manifests", severity=Severity.high,
                            description="No Kubernetes manifests found",
                            recommendation="Create deployment, service, and ingress manifests",
                        ),
                    ],
                ),
            ],
        )
        files = generate_source_patch_for_skill(skill, report, "pinky", llm_client=None)
        target_paths = {f.target_path for f in files}
        assert target_paths == {
            "helm/Chart.yaml", "helm/values.yaml",
            "helm/templates/deployment.yaml", "helm/templates/service.yaml",
        }
        for f in files:
            assert f.skill_name == "helm-chart"
            # Both open findings' descriptions are recorded as addressed —
            # one skill invocation clears both categories in one PR.
            assert "IaC tooling" in f.finding_addressed
            assert "Kubernetes manifests" in f.finding_addressed

    def test_helm_chart_clears_iac_and_manifests_findings_on_reassess(self, tmp_path: Path):
        """Functional/parity test: write the generated chart into a real
        repo tree and re-run the real analyzer -- both `iac` and
        `manifests` must disappear, exactly like infrastructure.py:41-56
        computes them (has_helm / has_k8s_manifests over iter_yaml_files)."""
        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        report = make_report(repo_name="pinky")
        files = generate_source_patch_for_skill(skill, report, "pinky", llm_client=None)
        for f in files:
            target = tmp_path / f.target_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")

        score = InfrastructureAnalyzer().analyze(tmp_path)
        cats = {f.category for f in score.findings}
        assert "iac" not in cats
        assert "manifests" not in cats

    def test_helm_chart_llm_tailored_multi_file_response_is_used(self):
        """A well-formed multi-file LLM response is parsed and used verbatim
        (not silently discarded in favor of the template fallback)."""
        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        report = make_report(repo_name="pinky")

        llm_response = (
            "===FILE: Chart.yaml===\n"
            "apiVersion: v2\n"
            "name: pinky\n"
            "version: 0.1.0\n"
            "===FILE: values.yaml===\n"
            "replicaCount: 2\n"
            "===FILE: templates/deployment.yaml===\n"
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: pinky\n"
            "spec:\n"
            "  replicas: \"{{ .Values.replicaCount }}\"\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: pinky\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: pinky\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: pinky\n"
            "          image: \"internal-registry/agentit/pinky:latest\"\n"
            "===FILE: templates/service.yaml===\n"
            "apiVersion: v1\n"
            "kind: Service\n"
            "metadata:\n"
            "  name: pinky\n"
            "spec:\n"
            "  selector:\n"
            "    app: pinky\n"
            "  ports:\n"
            "    - port: 8080\n"
            "===END===\n"
        )

        class _FakeLLM:
            def __init__(self, response: str) -> None:
                self.response = response
                self.calls = 0

            def _chat(self, system, user, max_tokens=None, **_kwargs):
                self.calls += 1
                return self.response

        fake_llm = _FakeLLM(llm_response)
        files = generate_source_patch_for_skill(skill, report, "pinky", llm_client=fake_llm)
        assert fake_llm.calls == 1
        by_path = {f.target_path: f.content for f in files}
        assert "replicaCount: 2" in by_path["helm/values.yaml"]
        assert '"{{ .Values.replicaCount }}"' in by_path["helm/templates/deployment.yaml"]
        for f in files:
            assert "LLM-tailored" in f.description

    def test_helm_chart_rejects_unquoted_helm_expression_that_breaks_yaml(self):
        """A real, easy-to-make Helm mistake: an unquoted `{{ .Values.x }}`
        starting a YAML scalar parses as YAML flow-mapping syntax, not a
        string, and fails validate_manifest() -- this must be rejected
        (falling back to the template), not shipped as a broken chart."""
        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        report = make_report(repo_name="pinky")

        class _FakeLLM:
            def _chat(self, system, user, max_tokens=None, **_kwargs):
                return (
                    "===FILE: Chart.yaml===\n"
                    "apiVersion: v2\nname: pinky\nversion: 0.1.0\n"
                    "===FILE: values.yaml===\n"
                    "replicaCount: 2\n"
                    "===FILE: templates/deployment.yaml===\n"
                    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: pinky\n"
                    "spec:\n  replicas: {{ .Values.replicaCount }}\n"
                    "===FILE: templates/service.yaml===\n"
                    "apiVersion: v1\nkind: Service\nmetadata:\n  name: pinky\n"
                    "===END===\n"
                )

        files = generate_source_patch_for_skill(skill, report, "pinky", llm_client=_FakeLLM())
        for f in files:
            assert "deterministic template" in f.description

    def test_helm_chart_falls_back_to_template_when_llm_output_invalid(self):
        """An LLM response missing Chart.yaml (or otherwise failing the
        real validation gate) must never ship -- this skill falls back to
        the deterministic template rather than a broken chart ("a wrong
        chart is worse than no chart")."""
        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        report = make_report(repo_name="pinky")

        class _FakeLLM:
            def _chat(self, system, user, max_tokens=None, **_kwargs):
                return "===FILE: templates/deployment.yaml===\nnot: a chart\n===END===\n"

        files = generate_source_patch_for_skill(skill, report, "pinky", llm_client=_FakeLLM())
        target_paths = {f.target_path for f in files}
        assert target_paths == {
            "helm/Chart.yaml", "helm/values.yaml",
            "helm/templates/deployment.yaml", "helm/templates/service.yaml",
        }
        for f in files:
            assert "deterministic template" in f.description

    def test_helm_chart_generated_templates_pass_manifest_validation(self):
        """Every templates/*.yaml file this skill emits (LLM or fallback)
        must pass the same validate_manifest() gate every other generated
        K8s manifest in this codebase passes before shipping."""
        from agentit.agents.base import validate_manifest

        skill = load_skill(Path("skills/infrastructure/helm-chart.md"))
        report = make_report(repo_name="pinky")
        files = generate_source_patch_for_skill(skill, report, "pinky", llm_client=None)
        for f in files:
            if f.target_path.startswith("helm/templates/"):
                assert validate_manifest(f.content) == []

    def test_skill_engine_generate_helm_chart_end_to_end(self):
        """SkillEngine.generate() for delivery: source routes correctly and
        sets target_path on every produced file (mirrors the containerfile
        parity test above, for the multi-file case)."""
        engine = SkillEngine(Path("skills"), platform=None)
        skill = engine.skill_for_category("iac")
        assert skill is not None
        assert skill.name == "helm-chart"
        assert engine.skill_for_category("manifests") is skill
        report = make_report(repo_name="pinky")
        report.scores = [
            DimensionScore(
                dimension="infrastructure", score=40, max_score=100,
                findings=[
                    Finding(
                        category="iac", severity=Severity.high,
                        description="No IaC tooling detected",
                        recommendation="Generate Helm chart",
                    ),
                    Finding(
                        category="manifests", severity=Severity.high,
                        description="No Kubernetes manifests found",
                        recommendation="Create manifests",
                    ),
                ],
            ),
        ]
        files = engine.generate(skill, report, llm_client=None)
        assert len(files) == 4
        assert all(f.target_path.startswith("helm/") for f in files)

    def test_one_skill_invocation_covers_both_iac_and_manifests(self):
        """SkillEngine.match() must resolve both open findings to the SAME
        skill instance (not two separate ones) -- run_all() should call
        the LLM/template generator only once for an app with both findings
        open, not duplicate the chart PR."""
        engine = SkillEngine(Path("skills"), platform=None)
        report = make_report(repo_name="pinky")
        report.scores = [
            DimensionScore(
                dimension="infrastructure", score=40, max_score=100,
                findings=[
                    Finding(category="iac", severity=Severity.high,
                            description="No IaC tooling detected", recommendation="x"),
                    Finding(category="manifests", severity=Severity.high,
                            description="No Kubernetes manifests found", recommendation="x"),
                ],
            ),
        ]
        matched = engine.match(report)
        helm_matches = [s for s in matched if s.name == "helm-chart"]
        assert len(helm_matches) == 1
