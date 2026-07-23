"""Clear-evidence simulation — founder bar: MERGE clears the finding."""
from __future__ import annotations

from agentit.portal.quality_prs import clear_evidence_simulation_ok
from agentit.remediation.clear_evidence import (
    ARGOCD_APPLICATION,
    AUDIT_WIRED,
    COSIGN_SIGN_TASK,
    GRAFANA_DASHBOARD,
    IMAGE_SCAN_TASK,
    SBOM_CI,
    SBOM_FILE,
    SELECTOR_TARGET,
    DOCKERFILE_PIN,
    HPA_TARGET,
    MIGRATION_TOOLING,
    simulate_finding_clearance,
    simulation_gate,
    verify_argocd_application,
    verify_audit_wired,
    verify_cosign_sign_task,
    verify_grafana_dashboard,
    verify_image_scan_task,
    verify_sbom_ci,
    verify_sbom_file,
    verify_dockerfile_pin,
    verify_hpa_target,
    verify_migration_tooling,
    verify_quota_manifest,
    verify_runtime_pin,
    verify_selector_target,
)


class TestDockerfilePin:
    def test_allows_pinned_from(self) -> None:
        ok, reason = verify_dockerfile_pin([{
            "target_path": "Dockerfile",
            "content": "FROM registry.access.redhat.com/ubi9/python-312:1\nUSER 1001\n",
            "skill_name": "containerfile",
        }])
        assert ok, reason
        assert ":latest" not in reason or "no :latest" in reason

    def test_refuses_latest(self) -> None:
        ok, reason = verify_dockerfile_pin([{
            "target_path": "Dockerfile",
            "content": "FROM ubi9/python-312:latest\nUSER 1001\n",
            "skill_name": "containerfile",
        }])
        assert not ok
        assert ":latest" in reason

    def test_refuses_destructive_rewrite_when_base_known(self) -> None:
        """#165 class: gutting a real Containerfile into a short stub."""
        existing = (
            "FROM registry.access.redhat.com/ubi9/python-312:latest\n"
            "USER 0\n"
            "RUN curl -sfL https://example.com/oc | tar -xz -C /usr/local/bin oc\n"
            "USER 1001\n"
            "WORKDIR /opt/app-root/src\n"
            "COPY pyproject.toml ./\n"
            "RUN pip install --no-cache-dir .\n"
            "COPY src/ src/\n"
            "COPY skills/ skills/\n"
            "COPY tests/ tests/\n"
            "HEALTHCHECK CMD curl -f http://localhost:8080/healthz || exit 1\n"
        )
        stub = (
            "FROM registry.access.redhat.com/ubi9/python-312:1\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "USER 1001\n"
            "EXPOSE 8080\n"
            "HEALTHCHECK CMD curl -f http://localhost:8080/healthz || exit 1\n"
        )
        ok, reason = verify_dockerfile_pin([{
            "target_path": "Containerfile",
            "content": stub,
            "base_content": existing,
            "skill_name": "containerfile",
        }])
        assert not ok
        assert "destructive" in reason.lower() or "guts" in reason.lower()

    def test_allows_pin_only_of_existing(self) -> None:
        from agentit.remediation.source_patches import pin_dockerfile_from_lines

        existing = (
            "FROM registry.access.redhat.com/ubi9/python-312:latest\n"
            "USER 0\n"
            "RUN curl -sfL https://example.com/oc | tar -xz -C /usr/local/bin oc\n"
            "USER 1001\n"
            "WORKDIR /opt/app-root/src\n"
            "COPY pyproject.toml ./\n"
            "RUN pip install --no-cache-dir .\n"
            "COPY src/ src/\n"
            "COPY skills/ skills/\n"
            "COPY tests/ tests/\n"
            "HEALTHCHECK CMD curl -f http://localhost:8080/healthz || exit 1\n"
        )
        pinned = pin_dockerfile_from_lines(existing)
        ok, reason = verify_dockerfile_pin([{
            "target_path": "Containerfile",
            "content": pinned,
            "base_content": existing,
            "skill_name": "containerfile",
        }])
        assert ok, reason
        assert ":latest" not in pinned
        assert "pip install" in pinned

    def test_refuses_unenriched_pin_only_marker(self) -> None:
        ok, reason = verify_dockerfile_pin([{
            "target_path": "Containerfile",
            "content": (
                "# agentit-pin-only: delivery will pin FROM on existing Containerfile\n"
                "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
            ),
            "skill_name": "containerfile",
        }])
        assert not ok
        assert "pin-only" in reason.lower() or "enrich" in reason.lower()

    def test_multi_dockerfile_overclaim_fails(self) -> None:
        """pulse-agent#2: pinning Dockerfile must not clear Dockerfile.deps/:latest."""
        files = [{
            "target_path": "Dockerfile",
            "content": "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\nUSER 1001\n",
            "skill_name": "containerfile",
        }]
        ok, reason = verify_dockerfile_pin(
            files,
            finding_description="Using :latest tag in base image in Dockerfile.deps",
        )
        assert not ok
        assert "Dockerfile.deps" in reason
        assert "not staged" in reason or "overclaim" in reason.lower()

    def test_single_file_pin_for_named_finding_passes(self) -> None:
        ok, reason = verify_dockerfile_pin(
            [{
                "target_path": "Dockerfile",
                "content": "FROM registry.access.redhat.com/ubi9/python-312:1\nUSER 1001\n",
                "skill_name": "containerfile",
            }],
            finding_description="Using :latest tag in base image in Dockerfile",
        )
        assert ok, reason

    def test_healthcheck_finding_not_cleared_by_from_pin(self) -> None:
        ok, reason = verify_dockerfile_pin(
            [{
                "target_path": "Dockerfile",
                "content": "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\nUSER 1001\n",
                "skill_name": "containerfile",
            }],
            finding_description="No HEALTHCHECK defined in Dockerfile",
        )
        assert not ok
        assert "HEALTHCHECK" in reason
        assert "mismatch" in reason.lower()

    def test_healthcheck_finding_cleared_when_directive_present(self) -> None:
        ok, reason = verify_dockerfile_pin(
            [{
                "target_path": "Dockerfile",
                "content": (
                    "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
                    "USER 1001\n"
                    "HEALTHCHECK CMD curl -f http://localhost:8080/healthz || exit 1\n"
                ),
                "skill_name": "containerfile",
            }],
            finding_description="No HEALTHCHECK defined in Dockerfile",
        )
        assert ok, reason

    def test_user_finding_cleared_when_non_root_user_present(self) -> None:
        ok, reason = verify_dockerfile_pin(
            [{
                "target_path": "Dockerfile",
                "content": (
                    "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\n"
                    "USER 1001\n"
                ),
                "skill_name": "containerfile",
            }],
            finding_description="Container runs as root (no USER directive) in Dockerfile",
        )
        assert ok, reason

    def test_non_ubi_finding_not_cleared_by_non_ubi_pin(self) -> None:
        ok, reason = verify_dockerfile_pin(
            [{
                "target_path": "Dockerfile.fast",
                "content": "FROM python:3.12\nUSER 1001\n",
                "skill_name": "containerfile",
            }],
            finding_description=(
                "Base image is not UBI (Red Hat Universal Base Image) in Dockerfile.fast"
            ),
        )
        assert not ok
        assert "ubi" in reason.lower() or "mismatch" in reason.lower()

    def test_non_ubi_finding_cleared_with_ubi_from(self) -> None:
        ok, reason = verify_dockerfile_pin(
            [{
                "target_path": "Dockerfile.fast",
                "content": (
                    "FROM registry.access.redhat.com/ubi9/python-312:1\n"
                    "USER 1001\n"
                ),
                "skill_name": "containerfile",
            }],
            finding_description=(
                "Base image is not UBI (Red Hat Universal Base Image) in Dockerfile.fast"
            ),
        )
        assert ok, reason


class TestAuditWired:
    def test_refuses_orphan_root_module(self) -> None:
        ok, reason = verify_audit_wired([{
            "target_path": "audit.py",
            "content": "def audit_log(*a, **k): ...\n",
            "skill_name": "app-audit-logging",
        }])
        assert not ok
        assert "root" in reason.lower() or "import" in reason.lower()

    def test_allows_packaged_module_plus_callsite(self) -> None:
        ok, reason = verify_audit_wired([
            {
                "target_path": "apps/api/src/pkg/audit.py",
                "content": (
                    '"""Application audit logging."""\n'
                    "import json\n"
                    "import logging\n"
                    "\n"
                    '_log = logging.getLogger("audit")\n'
                    "\n"
                    "def audit_log(action, *, actor, resource, outcome=\"success\"):\n"
                    "    record = {\n"
                    '        "ts": "2026-01-01T00:00:00+00:00",\n'
                    '        "type": "audit",\n'
                    '        "action": action,\n'
                    '        "actor": actor,\n'
                    '        "resource": resource,\n'
                    '        "outcome": outcome,\n'
                    "    }\n"
                    "    _log.info(\"%s\", json.dumps(record))\n"
                ),
                "skill_name": "app-audit-logging",
            },
            {
                "target_path": "apps/api/src/pkg/app.py",
                "content": (
                    "from .audit import audit_log\n"
                    "@app.middleware('http')\n"
                    "async def agentit_audit_middleware(request, call_next):\n"
                    "    return await call_next(request)\n"
                ),
                "skill_name": "app-audit-logging",
            },
        ])
        assert ok, reason

    def test_refuses_theater_stub_even_when_wired(self) -> None:
        """pinky #12: middleware wire-up must not launder a theater audit.py."""
        ok, reason = verify_audit_wired([
            {
                "target_path": "apps/api/src/pinky_api/audit.py",
                "content": (
                    '"""Theater stub — intentionally not wired into the app package."""\n'
                    "import logging\n"
                    'log = logging.getLogger("audit")\n'
                    "def audit_log(event, **kw):\n"
                    "    log.info(\"%s %s\", event, kw)\n"
                ),
                "skill_name": "audit-logging",
            },
            {
                "target_path": "apps/api/src/pinky_api/app.py",
                "content": (
                    "from pinky_api.audit import audit_log\n"
                    "async def agentit_audit_middleware(request, call_next):\n"
                    "    return await call_next(request)\n"
                ),
                "skill_name": "app-audit-logging",
            },
        ])
        assert not ok
        assert "theater" in reason.lower()


class TestRuntimePin:
    def test_allows_node_version(self) -> None:
        ok, reason = verify_runtime_pin([{
            "target_path": ".node-version",
            "content": "22\n",
            "skill_name": "eol-upgrade",
        }])
        assert ok, reason

    def test_refuses_empty(self) -> None:
        ok, _ = verify_runtime_pin([{
            "target_path": "README.md",
            "content": "hi\n",
        }])
        assert not ok


class TestHpaTarget:
    def test_allows_shaped_hpa(self) -> None:
        content = (
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "metadata:\n  name: pinky\n"
            "spec:\n"
            "  scaleTargetRef:\n"
            "    apiVersion: argoproj.io/v1alpha1\n"
            "    kind: Rollout\n"
            "    name: pinky\n"
            "  minReplicas: 2\n"
        )
        ok, reason = verify_hpa_target([{
            "path": "pinky-hpa.yaml",
            "content": content,
            "skill_name": "hpa",
        }])
        assert ok, reason

    def test_refuses_when_live_missing(self) -> None:
        content = (
            "apiVersion: autoscaling/v2\n"
            "kind: HorizontalPodAutoscaler\n"
            "spec:\n"
            "  scaleTargetRef:\n"
            "    kind: Deployment\n"
            "    name: pinky\n"
        )
        ok, reason = verify_hpa_target(
            [{"path": "hpa.yaml", "content": content, "skill_name": "hpa"}],
            live_workloads=[{"kind": "Deployment", "name": "pinky-api"}],
        )
        assert not ok
        assert "not in live" in reason


class TestQuotaManifest:
    def test_allows_resourcequota(self) -> None:
        ok, reason = verify_quota_manifest([{
            "path": "rq.yaml",
            "content": "apiVersion: v1\nkind: ResourceQuota\nmetadata:\n  name: q\n",
            "skill_name": "resourcequota",
        }])
        assert ok, reason


class TestMigrationTooling:
    def test_refuses_target_metadata_none_theater(self) -> None:
        ok, reason = verify_migration_tooling([
            {
                "target_path": "alembic.ini",
                "content": "[alembic]\nscript_location = alembic\n",
                "skill_name": "db-migration-tooling",
            },
            {
                "target_path": "alembic/env.py",
                "content": (
                    "from alembic import context\n"
                    "target_metadata = None\n"
                ),
                "skill_name": "db-migration-tooling",
            },
        ])
        assert not ok
        assert "theater" in reason.lower() or "target_metadata" in reason

    def test_allows_alembic_with_real_ddl_upgrade(self) -> None:
        ok, reason = verify_migration_tooling([
            {
                "target_path": "alembic.ini",
                "content": "[alembic]\nscript_location = alembic\n",
                "skill_name": "db-migration-tooling",
            },
            {
                "target_path": "alembic/env.py",
                "content": (
                    "import os\nfrom alembic import context\n"
                    "db_url = os.environ.get('DATABASE_URL')\n"
                    "target_metadata = None\n"
                ),
                "skill_name": "db-migration-tooling",
            },
            {
                "target_path": "alembic/versions/0001_baseline.py",
                "content": (
                    "from alembic import op\n"
                    "def upgrade():\n"
                    '    op.execute("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY)")\n'
                    "def downgrade():\n"
                    '    op.execute("DROP TABLE IF EXISTS t")\n'
                ),
                "skill_name": "db-migration-tooling",
            },
        ])
        assert ok, reason

    def test_refuses_empty_upgrade_pass(self) -> None:
        ok, reason = verify_migration_tooling([
            {
                "target_path": "alembic.ini",
                "content": "[alembic]\nscript_location = alembic\n",
                "skill_name": "db-migration-tooling",
            },
            {
                "target_path": "alembic/versions/0001_baseline.py",
                "content": "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n",
                "skill_name": "db-migration-tooling",
            },
        ])
        assert not ok
        assert "pass" in reason.lower() or "ddl" in reason.lower()

    def test_refuses_select_1_sql_stub(self) -> None:
        ok, reason = verify_migration_tooling([{
            "target_path": "migrations/0001_init.up.sql",
            "content": "-- baseline\nSELECT 1;\n",
            "skill_name": "db-migration-tooling",
        }])
        assert not ok
        assert "SELECT 1" in reason or "ddl" in reason.lower()

    def test_refuses_comment_only_op_execute(self) -> None:
        ok, reason = verify_migration_tooling([
            {
                "target_path": "alembic.ini",
                "content": "[alembic]\nscript_location = alembic\n",
                "skill_name": "db-migration-tooling",
            },
            {
                "target_path": "alembic/versions/0001_baseline.py",
                "content": (
                    "from alembic import op\n"
                    "def upgrade():\n"
                    '    op.execute("-- agentit baseline: add real DDL later")\n'
                    "def downgrade():\n    pass\n"
                ),
                "skill_name": "db-migration-tooling",
            },
        ])
        assert not ok
        assert "comment" in reason.lower() or "ddl" in reason.lower()

    def test_simulation_refuses_stub_migration_pr(self) -> None:
        files = [
            {
                "target_path": "alembic.ini",
                "content": "[alembic]\nscript_location = alembic\n",
                "skill_name": "db-migration-tooling",
            },
            {
                "target_path": "alembic/env.py",
                "content": "from alembic import context\ntarget_metadata = None\n",
                "skill_name": "db-migration-tooling",
            },
        ]
        ok, reason = clear_evidence_simulation_ok(
            files, [("migration", "No database migration tooling detected")],
        )
        assert not ok
        assert "migration" in reason


class TestSimulationGate:
    def test_allows_container_pin(self) -> None:
        files = [{
            "target_path": "Dockerfile",
            "content": "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\nUSER 1001\n",
            "skill_name": "containerfile",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files, [("container", "using :latest")],
        )
        assert ok, reason

    def test_refuses_container_still_latest(self) -> None:
        files = [{
            "target_path": "Dockerfile",
            "content": "FROM ubi:latest\nUSER 1001\n",
            "skill_name": "containerfile",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files, [("container", "using :latest")],
        )
        assert not ok
        assert "Clear-evidence simulation failed" in reason
        assert "container" in reason

    def test_pulse_agent_style_overclaim_refused(self) -> None:
        """Pinning only Dockerfile while targeting HEALTHCHECK + other Dockerfiles."""
        files = [{
            "target_path": "Dockerfile",
            "content": "FROM registry.access.redhat.com/ubi9/ubi-minimal:1\nUSER 1001\n",
            "skill_name": "containerfile",
        }]
        findings = [
            ("container", "Base image is not UBI (Red Hat Universal Base Image) in Dockerfile.fast"),
            ("container", "No HEALTHCHECK defined in Dockerfile"),
            ("container", "No HEALTHCHECK defined in Dockerfile.deps"),
            ("container", "No HEALTHCHECK defined in Dockerfile.fast"),
            ("container", "Using :latest tag in base image in Dockerfile"),
            ("container", "Using :latest tag in base image in Dockerfile.deps"),
            ("container", "Using :latest tag in base image in Dockerfile.fast"),
        ]
        ok, reason = clear_evidence_simulation_ok(files, findings)
        assert not ok
        assert "Clear-evidence simulation failed" in reason
        assert (
            "HEALTHCHECK" in reason
            or "Dockerfile.deps" in reason
            or "Dockerfile.fast" in reason
            or "mismatch" in reason.lower()
        )

    def test_refuses_detect_only_in_simulate_results(self) -> None:
        results = simulate_finding_clearance(
            [{"path": "LICENSE", "content": "Apache"}],
            [("license", "No LICENSE")],
        )
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].evidence_kind == "detect_only"

    def test_simulation_gate_all_must_pass(self) -> None:
        files = [{
            "target_path": "Dockerfile",
            "content": "FROM ubi:1\nUSER 1001\n",
            "skill_name": "containerfile",
        }]
        ok, reason, results = simulation_gate(
            files,
            [("container", "latest"), ("scaling", "no hpa")],
        )
        assert not ok
        assert any(r.category == "scaling" and not r.ok for r in results)
        assert "scaling" in reason

    def test_evidence_kind_on_contract(self) -> None:
        from agentit.remediation.registry import contract_for

        assert contract_for("container").evidence_kind == DOCKERFILE_PIN
        assert contract_for("audit").evidence_kind == AUDIT_WIRED
        assert contract_for("scaling").evidence_kind == HPA_TARGET
        assert contract_for("migration").evidence_kind == MIGRATION_TOOLING
        assert contract_for("image_signing").evidence_kind == COSIGN_SIGN_TASK
        assert contract_for("sbom").evidence_kind == SBOM_CI
        assert contract_for("scanning").evidence_kind == IMAGE_SCAN_TASK
        assert contract_for("dashboards").evidence_kind == GRAFANA_DASHBOARD
        assert contract_for("availability").evidence_kind == SELECTOR_TARGET
        assert contract_for("metrics").evidence_kind == SELECTOR_TARGET
        assert contract_for("gitops").evidence_kind == ARGOCD_APPLICATION


class TestCosignSignTask:
    def test_allows_cosign_sign_task(self) -> None:
        ok, reason = verify_cosign_sign_task([{
            "target_path": "apps/pinky/cosign-sign-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\n"
                "kind: Task\n"
                "metadata:\n  name: pinky-cosign-sign\n"
                "spec:\n  steps:\n"
                "  - name: cosign-sign\n"
                "    image: gcr.io/projectsigstore/cosign:v2.4.3\n"
                "    script: |\n"
                "      cosign sign --yes $(params.IMAGE)\n"
            ),
            "skill_name": "cosign-sign-task",
        }])
        assert ok, reason

    def test_refuses_empty_task_theater(self) -> None:
        ok, reason = verify_cosign_sign_task([{
            "target_path": "apps/pinky/sign-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\n"
                "kind: Task\n"
                "metadata:\n  name: pretend-sign\n"
                "spec:\n  steps: []\n"
            ),
            "skill_name": "cosign-sign-task",
        }])
        assert not ok
        assert "cosign" in reason.lower() or "theater" in reason.lower()

    def test_refuses_slsa_hermetic_theater(self) -> None:
        ok, reason = verify_cosign_sign_task([{
            "target_path": "apps/pinky/slsa-l3.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\n"
                "kind: Task\n"
                "metadata:\n"
                "  name: hermetic-konflux-slsa-l3\n"
                "  annotations:\n"
                "    description: Claims SLSA Level 3 hermetic Konflux build\n"
                "spec:\n  steps:\n"
                "  - name: noop\n"
                "    image: registry.access.redhat.com/ubi9-minimal:latest\n"
                "    script: echo theater\n"
            ),
            "skill_name": "cosign-sign-task",
        }])
        assert not ok
        assert "theater" in reason.lower() or "slsa" in reason.lower() or "hermetic" in reason.lower()

    def test_simulation_allows_signing_pr(self) -> None:
        files = [{
            "target_path": "chart/templates/tekton/cosign-sign-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\n"
                "kind: Task\n"
                "metadata:\n  name: app-cosign-sign\n"
                "spec:\n  steps:\n"
                "  - name: sign\n"
                "    image: gcr.io/projectsigstore/cosign:v2.4.3\n"
                "    script: cosign sign --yes $(params.IMAGE)\n"
            ),
            "skill_name": "cosign-sign-task",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files,
            [("image_signing", "No cosign/Sigstore image signing detected in CI or Tekton")],
        )
        assert ok, reason

    def test_simulation_refuses_scan_task_as_signing(self) -> None:
        files = [{
            "target_path": "apps/pinky/image-scan-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\n"
                "kind: Task\n"
                "metadata:\n  name: image-scan\n"
                "spec:\n  steps:\n"
                "  - name: scan\n"
                "    image: aquasec/trivy:latest\n"
                "    script: trivy image $(params.IMAGE)\n"
            ),
            "skill_name": "image-scan-task",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files,
            [("image_signing", "No cosign/Sigstore image signing detected")],
        )
        assert not ok
        assert "image_signing" in reason or "Clear-evidence" in reason


class TestSbomCi:
    _GHA = (
        "name: SBOM\non: [push]\njobs:\n  sbom:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "      - uses: anchore/sbom-action@v0.24.0\n"
        "        with:\n          format: cyclonedx-json\n"
    )
    _REAL_CDX = (
        '{"bomFormat":"CycloneDX","specVersion":"1.5",'
        '"components":[{"type":"library","name":"flask","version":"3.0.0",'
        '"purl":"pkg:pypi/flask@3.0.0"}]}\n'
    )

    def test_allows_gha_sbom_action(self) -> None:
        ok, reason = verify_sbom_ci([{
            "target_path": ".github/workflows/sbom.yml",
            "content": self._GHA,
            "skill_name": "sbom-ci",
        }])
        assert ok, reason
        assert "anchore/sbom-action" in reason

    def test_allows_tekton_pipeline_wire(self) -> None:
        ok, reason = verify_sbom_ci([{
            "target_path": "app-sbom-pipeline.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\nkind: Pipeline\n"
                "metadata:\n  name: app-pipeline\n"
                "spec:\n  tasks:\n"
                "    - name: sbom-generate\n"
                "      taskRef:\n        name: app-sbom\n"
            ),
            "skill_name": "sbom-ci",
        }])
        assert ok, reason

    def test_refuses_static_cyclonedx(self) -> None:
        ok, reason = verify_sbom_ci([{
            "target_path": "sbom.cdx.json",
            "content": self._REAL_CDX,
            "skill_name": "sbom-artifact",
        }])
        assert not ok
        assert "static" in reason.lower() or "CI" in reason

    def test_refuses_bare_sbom_task(self) -> None:
        ok, reason = verify_sbom_ci([{
            "target_path": "apps/pinky/sbom-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\nkind: Task\n"
                "metadata:\n  name: pinky-sbom\n"
                "spec:\n  steps:\n"
                "  - name: generate-sbom\n"
                "    image: anchore/syft:v1.48.0\n"
                "    args: [$(params.IMAGE), --output, cyclonedx-json=/ws/sbom.json]\n"
            ),
            "skill_name": "sbom-task",
        }])
        assert not ok
        assert "bare" in reason.lower() or "Task" in reason

    def test_simulation_allows_gha_pr(self) -> None:
        files = [{
            "target_path": ".github/workflows/sbom.yml",
            "content": self._GHA,
            "skill_name": "sbom-ci",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files, [("sbom", "No SBOM generation in CI")],
        )
        assert ok, reason

    def test_simulation_refuses_static_file_pr(self) -> None:
        files = [{
            "target_path": "sbom.cdx.json",
            "content": self._REAL_CDX,
            "skill_name": "sbom-artifact",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files, [("sbom", "No SBOM generation in CI")],
        )
        assert not ok
        assert "sbom" in reason.lower()

    def test_legacy_verify_sbom_file_still_accepts_components(self) -> None:
        # Demoted fallback helper — not the contract evidence_kind.
        ok, reason = verify_sbom_file([{
            "target_path": "sbom.cdx.json",
            "content": self._REAL_CDX,
            "skill_name": "sbom-artifact",
        }])
        assert ok, reason
        assert SBOM_FILE == "sbom_file"


_GOOD_SCAN_TASK = (
    "apiVersion: tekton.dev/v1\n"
    "kind: Task\n"
    "metadata:\n  name: image-scan\n"
    "spec:\n  steps:\n"
    "  - name: scan\n"
    "    image: aquasec/trivy:0.58.1\n"
    "    script: |\n"
    "      trivy image $(params.IMAGE)\n"
    "  - name: report\n"
    "    image: registry.access.redhat.com/ubi9/ubi-minimal:9.5\n"
    "    script: echo ok\n"
)


class TestImageScanTask:
    def test_allows_pinned_trivy_task(self) -> None:
        ok, reason = verify_image_scan_task([{
            "target_path": "apps/pinky/image-scan-task.yaml",
            "content": _GOOD_SCAN_TASK,
            "skill_name": "image-scan-task",
        }])
        assert ok, reason

    def test_refuses_empty_task(self) -> None:
        ok, reason = verify_image_scan_task([{
            "target_path": "apps/pinky/image-scan-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\nkind: Task\n"
                "metadata:\n  name: empty-scan\n"
                "spec:\n  steps: []\n"
            ),
            "skill_name": "image-scan-task",
        }])
        assert not ok
        assert "empty" in reason.lower() or "trivy" in reason.lower()

    def test_refuses_latest_step_images(self) -> None:
        ok, reason = verify_image_scan_task([{
            "target_path": "apps/pinky/image-scan-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\nkind: Task\n"
                "metadata:\n  name: image-scan\n"
                "spec:\n  steps:\n"
                "  - name: scan\n"
                "    image: aquasec/trivy:latest\n"
                "    script: trivy image $(params.IMAGE)\n"
            ),
            "skill_name": "image-scan-task",
        }])
        assert not ok
        assert ":latest" in reason

    def test_simulation_allows_good_scan_pr(self) -> None:
        files = [{
            "target_path": "apps/pinky/image-scan-task.yaml",
            "content": _GOOD_SCAN_TASK,
            "skill_name": "image-scan-task",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files, [("scanning", "No image vulnerability scanning")],
        )
        assert ok, reason

    def test_simulation_refuses_latest_as_scanning(self) -> None:
        files = [{
            "target_path": "apps/pinky/image-scan-task.yaml",
            "content": (
                "apiVersion: tekton.dev/v1\nkind: Task\n"
                "metadata:\n  name: image-scan\n"
                "spec:\n  steps:\n"
                "  - name: scan\n"
                "    image: aquasec/trivy:latest\n"
                "    script: trivy image $(params.IMAGE)\n"
            ),
            "skill_name": "image-scan-task",
        }]
        ok, reason = clear_evidence_simulation_ok(
            files, [("scanning", "No image vulnerability scanning")],
        )
        assert not ok
        assert "scanning" in reason or ":latest" in reason


class TestGrafanaDashboard:
    _GOOD_CM = (
        "apiVersion: v1\nkind: ConfigMap\n"
        "metadata:\n  name: app-grafana-dashboard\n"
        "  labels:\n    grafana_dashboard: \"1\"\n"
        "data:\n  dash.json: |\n"
        '    {"title":"app","panels":[{"title":"Rate","type":"timeseries"}]}\n'
    )

    def test_allows_labeled_dashboard_with_panels(self) -> None:
        ok, reason = verify_grafana_dashboard([{
            "target_path": "apps/pinky/grafana-dashboard.yaml",
            "content": self._GOOD_CM,
            "skill_name": "grafana-dashboard",
        }])
        assert ok, reason

    def test_refuses_missing_label(self) -> None:
        ok, reason = verify_grafana_dashboard([{
            "target_path": "apps/pinky/grafana-dashboard.yaml",
            "content": (
                "apiVersion: v1\nkind: ConfigMap\n"
                "metadata:\n  name: app-dash\n"
                "data:\n  dash.json: |\n"
                '    {"panels":[{"title":"x"}]}\n'
            ),
            "skill_name": "grafana-dashboard",
        }])
        assert not ok
        assert "grafana_dashboard" in reason

    def test_refuses_empty_panels(self) -> None:
        ok, reason = verify_grafana_dashboard([{
            "target_path": "apps/pinky/grafana-dashboard.yaml",
            "content": (
                "apiVersion: v1\nkind: ConfigMap\n"
                "metadata:\n  name: app-dash\n"
                "  labels:\n    grafana_dashboard: \"1\"\n"
                "data:\n  dash.json: |\n"
                '    {"title":"app","panels":[]}\n'
            ),
            "skill_name": "grafana-dashboard",
        }])
        assert not ok
        assert "panels" in reason.lower() or "empty" in reason.lower()

    def test_simulation_allows_good_dashboard(self) -> None:
        ok, reason = clear_evidence_simulation_ok(
            [{
                "target_path": "apps/pinky/grafana-dashboard.yaml",
                "content": self._GOOD_CM,
                "skill_name": "grafana-dashboard",
            }],
            [("dashboards", "No Grafana dashboard")],
        )
        assert ok, reason


class TestSelectorTarget:
    _PDB = (
        "apiVersion: policy/v1\nkind: PodDisruptionBudget\n"
        "metadata:\n  name: app\n"
        "spec:\n  minAvailable: 1\n"
        "  selector:\n    matchLabels:\n      app: pinky\n"
    )
    _SM = (
        "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\n"
        "metadata:\n  name: app-monitor\n"
        "spec:\n  selector:\n    matchLabels:\n      app: pinky\n"
        "  endpoints:\n    - port: http\n"
    )

    def test_allows_pdb_shape(self) -> None:
        ok, reason = verify_selector_target(
            [{"target_path": "pdb.yaml", "content": self._PDB, "skill_name": "pdb"}],
            frozenset({"PodDisruptionBudget"}),
        )
        assert ok, reason

    def test_refuses_empty_selector(self) -> None:
        ok, reason = verify_selector_target(
            [{
                "target_path": "pdb.yaml",
                "content": (
                    "apiVersion: policy/v1\nkind: PodDisruptionBudget\n"
                    "metadata:\n  name: app\n"
                    "spec:\n  minAvailable: 1\n  selector: {}\n"
                ),
                "skill_name": "pdb",
            }],
            frozenset({"PodDisruptionBudget"}),
        )
        assert not ok
        assert "empty" in reason.lower() or "matchLabels" in reason

    def test_refuses_zero_match_live(self) -> None:
        ok, reason = verify_selector_target(
            [{"target_path": "pdb.yaml", "content": self._PDB, "skill_name": "pdb"}],
            frozenset({"PodDisruptionBudget"}),
            live_label_sets=[{"app": "other"}],
        )
        assert not ok
        assert "zero-match" in reason or "matches no live" in reason

    def test_allows_live_match(self) -> None:
        ok, reason = verify_selector_target(
            [{"target_path": "sm.yaml", "content": self._SM, "skill_name": "service-monitor"}],
            frozenset({"ServiceMonitor"}),
            live_label_sets=[{"app": "pinky", "tier": "api"}],
        )
        assert ok, reason

    def test_refuses_empty_live_sets(self) -> None:
        ok, reason = verify_selector_target(
            [{"target_path": "sm.yaml", "content": self._SM, "skill_name": "service-monitor"}],
            frozenset({"ServiceMonitor"}),
            live_label_sets=[],
        )
        assert not ok
        assert "zero-match" in reason or "no live" in reason

    def test_simulation_refuses_pdb_zero_match(self) -> None:
        ok, reason, results = simulation_gate(
            [{"target_path": "pdb.yaml", "content": self._PDB, "skill_name": "pdb"}],
            [("availability", "No PodDisruptionBudget")],
            live_label_sets=[{"app": "wrong"}],
        )
        assert not ok
        assert any(r.category == "availability" and not r.ok for r in results)


class TestArgocdApplication:
    _GOOD = (
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\n"
        "metadata:\n  name: pinky\n  namespace: openshift-gitops\n"
        "spec:\n  project: default\n"
        "  source:\n"
        "    repoURL: https://github.com/org/pinky.git\n"
        "    path: chart/\n"
        "  destination:\n"
        "    server: https://kubernetes.default.svc\n"
        "    namespace: pinky\n"
    )

    def test_allows_repo_url_and_path(self) -> None:
        ok, reason = verify_argocd_application([{
            "target_path": "apps/pinky/argocd-application.yaml",
            "content": self._GOOD,
            "skill_name": "argocd-application",
        }])
        assert ok, reason

    def test_refuses_missing_repo_url(self) -> None:
        ok, reason = verify_argocd_application([{
            "target_path": "app.yaml",
            "content": (
                "apiVersion: argoproj.io/v1alpha1\nkind: Application\n"
                "metadata:\n  name: x\n"
                "spec:\n  source:\n    path: chart/\n"
            ),
            "skill_name": "argocd-application",
        }])
        assert not ok
        assert "repoURL" in reason

    def test_refuses_missing_path_and_chart(self) -> None:
        ok, reason = verify_argocd_application([{
            "target_path": "app.yaml",
            "content": (
                "apiVersion: argoproj.io/v1alpha1\nkind: Application\n"
                "metadata:\n  name: x\n"
                "spec:\n  source:\n"
                "    repoURL: https://github.com/org/x.git\n"
            ),
            "skill_name": "argocd-application",
        }])
        assert not ok
        assert "path" in reason or "chart" in reason

    def test_refuses_bogus_deploy_when_tree_missing(self) -> None:
        ok, reason = verify_argocd_application(
            [{
                "target_path": "app.yaml",
                "content": (
                    "apiVersion: argoproj.io/v1alpha1\nkind: Application\n"
                    "metadata:\n  name: x\n"
                    "spec:\n  source:\n"
                    "    repoURL: https://github.com/org/x.git\n"
                    "    path: deploy/\n"
                ),
                "skill_name": "argocd-application",
            }],
            tree_paths=["README.md", "src/main.go", "chart/Chart.yaml"],
        )
        assert not ok
        assert "deploy" in reason.lower() or "missing" in reason.lower()

    def test_allows_chart_path_when_in_tree(self) -> None:
        ok, reason = verify_argocd_application(
            [{
                "target_path": "app.yaml",
                "content": self._GOOD,
                "skill_name": "argocd-application",
            }],
            tree_paths=["chart/Chart.yaml", "chart/templates/deploy.yaml"],
        )
        assert ok, reason

    def test_simulation_allows_good_application(self) -> None:
        ok, reason = clear_evidence_simulation_ok(
            [{
                "target_path": "apps/pinky/argocd-application.yaml",
                "content": self._GOOD,
                "skill_name": "argocd-application",
            }],
            [("gitops", "No Argo CD Application")],
        )
        assert ok, reason
