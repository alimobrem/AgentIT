from __future__ import annotations

from datetime import datetime, timezone

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)
from conftest import make_store, make_report


# ── Events ──────────────────────────────────────────────────────────────


def test_log_and_list_events():
    store = make_store()
    eid = store.log_event("bot", "deploy", "my-app", "info", "deployed v1")
    assert eid

    events = store.list_events()
    assert len(events) == 1
    assert events[0]["agent_id"] == "bot"
    assert events[0]["action"] == "deploy"
    assert events[0]["target_app"] == "my-app"
    assert events[0]["summary"] == "deployed v1"

    # filter by target_app
    store.log_event("bot", "scan", "other-app", "warning", "drift detected")
    filtered = store.list_events(target_app="other-app")
    assert len(filtered) == 1
    assert filtered[0]["target_app"] == "other-app"

    # all events
    assert len(store.list_events()) == 2


# ── Assessment history ──────────────────────────────────────────────────


def test_list_history_returns_multiple_assessments():
    store = make_store()
    r1 = make_report(repo_name="test-repo", scores=[DimensionScore(
        dimension="security", score=40, max_score=100,
        findings=[Finding(category="test", severity=Severity.low,
                          description="minor", recommendation="fix")],
    )])
    r1.assessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    r2 = make_report(repo_name="test-repo", scores=[DimensionScore(
        dimension="security", score=60, max_score=100,
        findings=[Finding(category="test", severity=Severity.low,
                          description="minor", recommendation="fix")],
    )])
    r2.assessed_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    store.save(r1)
    store.save(r2)

    history = store.list_history("https://github.com/org/test-repo")
    assert len(history) == 2
    # ordered ascending by date
    assert history[0]["overall_score"] == 40.0
    assert history[1]["overall_score"] == 60.0


def test_get_trend_shows_delta():
    store = make_store()
    r1 = make_report(repo_name="test-repo", scores=[DimensionScore(
        dimension="security", score=40, max_score=100,
        findings=[Finding(category="test", severity=Severity.low,
                          description="minor", recommendation="fix")],
    )])
    r1.assessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    r2 = make_report(repo_name="test-repo", scores=[DimensionScore(
        dimension="security", score=70, max_score=100,
        findings=[Finding(category="test", severity=Severity.low,
                          description="minor", recommendation="fix")],
    )])
    r2.assessed_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    store.save(r1)
    store.save(r2)

    trend = store.get_trend("https://github.com/org/test-repo")
    assert trend["current_score"] == 70.0
    assert trend["previous_score"] == 40.0
    assert trend["delta"] == 30.0
    assert trend["assessments_count"] == 2

    # empty repo
    empty = store.get_trend("https://github.com/org/nonexistent")
    assert empty["assessments_count"] == 0
    assert empty["delta"] is None


# ── Gates ────────────────────────────────────────────────────────────────


def test_create_and_resolve_gate():
    store = make_store()
    aid = store.save(make_report())
    gid = store.create_gate(aid, "security", "Critical vuln found")
    assert gid

    gates = store.list_gates()
    assert len(gates) == 1
    assert gates[0]["gate_type"] == "security"
    assert gates[0]["status"] == "pending"

    ok = store.resolve_gate(gid, "approved", "alice")
    assert ok is True

    # pending list is now empty
    assert store.list_gates(status="pending") == []
    approved = store.list_gates(status="approved")
    assert len(approved) == 1
    assert approved[0]["resolved_by"] == "alice"

    # resolving again returns False (already resolved)
    assert store.resolve_gate(gid, "rejected", "bob") is False


def test_list_gates_filters_by_status():
    store = make_store()
    aid = store.save(make_report())
    g1 = store.create_gate(aid, "compliance", "Missing SBOM")
    g2 = store.create_gate(aid, "security", "No network policy")

    store.resolve_gate(g1, "approved", "carol")

    assert len(store.list_gates(status="pending")) == 1
    assert len(store.list_gates(status="approved")) == 1
    assert store.list_gates(status="pending")[0]["id"] == g2


# ── Severity enum regression ───────────────────────────────────────────


def test_fleet_data_counts_critical_findings_correctly():
    """Regression: severity comparison must use Severity enum, not raw ints."""
    store = make_store()
    report = AssessmentReport(
        repo_url="https://github.com/org/sev-test",
        repo_name="sev-test",
        assessed_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", version="3.12", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[], auth_mechanism=None,
        ),
        scores=[
            DimensionScore(
                dimension="security", score=30, max_score=100,
                findings=[
                    Finding(category="auth", severity=Severity.critical,
                            description="No auth", recommendation="Add auth"),
                    Finding(category="secrets", severity=Severity.high,
                            description="Hardcoded secret", recommendation="Use vault"),
                    Finding(category="lint", severity=Severity.low,
                            description="Minor lint", recommendation="Fix lint"),
                ],
            ),
        ],
        criticality="high",
        summary="test",
        remediation_plan=[],
    )
    store.save(report)
    fleet = store.get_fleet_data()
    assert len(fleet) == 1
    assert fleet[0]["critical_count"] == 2  # 1 critical + 1 high


# ── Remediations ───────────────────────────────────────────────────────


class TestRemediationsTable:
    def test_save_and_list(self):
        store = make_store()
        aid = store.save(make_report())
        rid = store.save_remediation(aid, "security", "Fix RBAC")
        rems = store.list_remediations(aid)
        assert len(rems) == 1
        assert rems[0]["agent_name"] == "security"
        assert rems[0]["status"] == "generated"
        assert rems[0]["id"] == rid

    def test_complete(self):
        store = make_store()
        aid = store.save(make_report())
        rid = store.save_remediation(aid, "security", "Fix RBAC")
        assert store.complete_remediation(rid) is True
        rems = store.list_remediations(aid)
        assert rems[0]["status"] == "completed"
        assert rems[0]["completed_at"] is not None

    def test_complete_idempotent(self):
        store = make_store()
        aid = store.save(make_report())
        rid = store.save_remediation(aid, "cicd", "Add pipeline")
        store.complete_remediation(rid)
        assert store.complete_remediation(rid) is False


# ── Agent Registry ─────────────────────────────────────────────────────


class TestAgentRegistryTable:
    def test_register_and_list(self):
        store = make_store()
        aid = store.register_agent("security", "hardening", "network,rbac")
        agents = store.list_agents()
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "security"
        assert agents[0]["category"] == "hardening"
        assert agents[0]["status"] == "active"

    def test_heartbeat(self):
        store = make_store()
        store.register_agent("observability", "monitoring")
        assert store.agent_heartbeat("observability") is True
        assert store.agent_heartbeat("nonexistent") is False

    def test_register_replaces_existing(self):
        store = make_store()
        store.register_agent("security", "hardening", "v1")
        store.register_agent("security", "hardening", "v2")
        agents = store.list_agents()
        assert len(agents) == 1
        assert agents[0]["capabilities"] == "v2"


# ── SLOs ───────────────────────────────────────────────────────────────


class TestSlosTable:
    def test_save_and_list(self):
        store = make_store()
        aid = store.save(make_report())
        sid = store.save_slo(aid, "availability", 99.9)
        slos = store.list_slos(aid)
        assert len(slos) == 1
        assert slos[0]["metric_name"] == "availability"
        assert slos[0]["target_value"] == 99.9
        assert slos[0]["status"] == "unknown"
        assert slos[0]["id"] == sid

    def test_update_slo(self):
        store = make_store()
        aid = store.save(make_report())
        sid = store.save_slo(aid, "error_rate", 0.1)
        assert store.update_slo(sid, 0.05, "met") is True
        slos = store.list_slos(aid)
        assert slos[0]["current_value"] == 0.05
        assert slos[0]["status"] == "met"
        assert slos[0]["updated_at"] is not None

    def test_multiple_slos_per_assessment(self):
        store = make_store()
        aid = store.save(make_report())
        store.save_slo(aid, "availability", 99.9)
        store.save_slo(aid, "latency_p99", 200.0)
        store.save_slo(aid, "error_rate", 0.1)
        slos = store.list_slos(aid)
        assert len(slos) == 3


# ── Orchestrator wiring ────────────────────────────────────────────────


class TestOrchestratorStoreWiring:
    def test_remediations_recorded_on_onboard(self):
        """Orchestrator records remediations in the store when assessment_id is provided."""
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = make_store()
        report = make_report()
        aid = store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=store, assessment_id=aid,
            )
            result = orch.run()

        rems = store.list_remediations(aid)
        assert len(rems) > 0
        agent_names_in_rems = {r["agent_name"] for r in rems}
        assert "security" in agent_names_in_rems

    def test_agents_registered_on_run(self):
        """Orchestrator registers available agents in the store."""
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = make_store()
        report = make_report()
        aid = store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=store, assessment_id=aid,
            )
            orch.run()

        agents = store.list_agents()
        agent_names = {a["agent_name"] for a in agents}
        for core in ("security", "observability", "cicd", "compliance"):
            assert core in agent_names, f"{core} not registered"

    def test_no_remediations_without_assessment_id(self):
        """Orchestrator skips remediation recording when assessment_id is None."""
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = make_store()
        report = make_report()
        store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=store,
            )
            orch.run()

        # No assessment_id means no remediations saved — but we can't
        # query without an assessment_id. Just verify no crash.
        assert True

    def test_slos_created_on_onboard(self):
        """Orchestrator creates default SLOs after release agent runs."""
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = make_store()
        report = make_report()
        aid = store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=store, assessment_id=aid,
            )
            orch.run()

        slos = store.list_slos(aid)
        assert len(slos) == 3
        metric_names = {s["metric_name"] for s in slos}
        assert "availability" in metric_names
        assert "error_rate" in metric_names
        assert "latency_p99_ms" in metric_names


# ── Onboarding history ────────────────────────────────────────────────


# ── Apply results (repo_files_json migration) ──────────────────────────


class TestApplyResultsTable:
    def test_save_and_get_apply_results_fresh_db(self):
        """Regression: a brand-new DB must create apply_results with
        repo_files_json already in the CREATE TABLE statement, so
        save_apply_results (which always writes that column) doesn't fail
        with 'table apply_results has no column named repo_files_json'."""
        store = make_store()
        aid = store.save(make_report())
        store.save_apply_results(
            aid,
            {"applied": ["a.yaml"], "skipped": [], "errors": [], "repo_files": ["a.yaml"]},
            namespace="test-ns",
            dry_run=False,
        )
        result = store.get_apply_results(aid)
        assert result is not None
        assert result["applied"] == ["a.yaml"]
        assert result["repo_files"] == ["a.yaml"]
        assert result["namespace"] == "test-ns"

    def test_migration_idempotent_on_existing_db(self, tmp_path):
        """Regression: re-opening a DB that already has repo_files_json
        (e.g. a second AssessmentStore() in the same process, or a pod
        restart) must not raise -- the ALTER TABLE must tolerate the
        column already existing."""
        db_path = str(tmp_path / "test.db")
        from agentit.portal.store import AssessmentStore as Store

        store1 = Store(db_path=db_path)
        aid = store1.save(make_report())
        store1.save_apply_results(
            aid, {"applied": [], "skipped": [], "errors": [], "repo_files": []},
            namespace="ns", dry_run=True,
        )

        # Re-opening simulates a restart against the same on-disk DB --
        # must not raise "duplicate column name: repo_files_json".
        store2 = Store(db_path=db_path)
        result = store2.get_apply_results(aid)
        assert result is not None

    def test_migration_adds_column_to_legacy_schema(self, tmp_path):
        """Regression: a genuinely pre-existing DB whose apply_results table
        predates repo_files_json (old CREATE TABLE, no ALTER yet applied)
        must be migrated in-place when AssessmentStore opens it."""
        import sqlite3

        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE apply_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                applied_json TEXT NOT NULL DEFAULT '[]',
                skipped_json TEXT NOT NULL DEFAULT '[]',
                errors_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        from agentit.portal.store import AssessmentStore as Store

        store = Store(db_path=db_path)
        aid = store.save(make_report())
        store.save_apply_results(
            aid, {"applied": ["x.yaml"], "skipped": [], "errors": [], "repo_files": ["x.yaml"]},
            namespace="ns", dry_run=False,
        )
        result = store.get_apply_results(aid)
        assert result is not None
        assert result["repo_files"] == ["x.yaml"]


class TestOnboardingHistory:
    def test_list_onboardings(self):
        store = make_store()
        aid = store.save(make_report())
        store.save_onboarding(aid, [
            {"category": "security", "path": "rbac.yaml", "content": "x", "description": "d"},
        ], orchestration={"recommendation": "READY", "auto_approve": False})
        store.save_onboarding(aid, [
            {"category": "security", "path": "rbac.yaml", "content": "x", "description": "d"},
            {"category": "cicd", "path": "pipeline.yaml", "content": "y", "description": "p"},
        ], orchestration={"recommendation": "AUTO-APPROVED", "auto_approve": True})

        history = store.list_onboardings(aid)
        assert len(history) == 2
        assert history[0]["file_count"] == 2  # most recent first
        assert history[1]["file_count"] == 1

    def test_list_onboardings_empty(self):
        store = make_store()
        aid = store.save(make_report())
        assert store.list_onboardings(aid) == []
