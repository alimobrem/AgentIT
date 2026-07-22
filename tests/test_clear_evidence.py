"""Clear-evidence simulation — founder bar: MERGE clears the finding."""
from __future__ import annotations

from agentit.portal.quality_prs import clear_evidence_simulation_ok
from agentit.remediation.clear_evidence import (
    AUDIT_WIRED,
    DOCKERFILE_PIN,
    HPA_TARGET,
    MIGRATION_TOOLING,
    simulate_finding_clearance,
    simulation_gate,
    verify_audit_wired,
    verify_dockerfile_pin,
    verify_hpa_target,
    verify_migration_tooling,
    verify_quota_manifest,
    verify_runtime_pin,
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
                "content": "def audit_log(*a, **k): pass\n",
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

    def test_allows_alembic_with_upgrade_revision(self) -> None:
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
                "content": "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n",
                "skill_name": "db-migration-tooling",
            },
        ])
        assert ok, reason

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
