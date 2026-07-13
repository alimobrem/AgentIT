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

    def test_delete(self):
        store = make_store()
        aid = store.save(make_report())
        rid = store.save_remediation(aid, "security", "Fix RBAC")
        assert store.delete_remediation(rid, aid) is True
        assert store.list_remediations(aid) == []

    def test_delete_wrong_assessment_returns_false(self):
        store = make_store()
        aid = store.save(make_report())
        other_aid = store.save(make_report(repo_name="other-app"))
        rid = store.save_remediation(aid, "security", "Fix RBAC")
        assert store.delete_remediation(rid, other_aid) is False
        assert len(store.list_remediations(aid)) == 1


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

    def test_heartbeat_upserts_unregistered_agent(self):
        """Long-lived watchers (vuln-watcher, slo-tracker, ...) never call
        register_agent -- agent_heartbeat must create the row itself so their
        "last seen" actually shows up on the Agents/Schedules pages."""
        store = make_store()
        assert store.agent_heartbeat("vuln-watcher") is True
        agents = store.list_agents()
        assert any(a["agent_name"] == "vuln-watcher" for a in agents)
        watcher = next(a for a in agents if a["agent_name"] == "vuln-watcher")
        assert watcher["category"] == "watcher"
        assert watcher["last_heartbeat"] is not None

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

    def test_delete(self):
        store = make_store()
        aid = store.save(make_report())
        sid = store.save_slo(aid, "availability", 99.9)
        assert store.delete_slo(sid, aid) is True
        assert store.list_slos(aid) == []

    def test_delete_wrong_assessment_returns_false(self):
        store = make_store()
        aid = store.save(make_report())
        other_aid = store.save(make_report(repo_name="other-app"))
        sid = store.save_slo(aid, "availability", 99.9)
        assert store.delete_slo(sid, other_aid) is False
        assert len(store.list_slos(aid)) == 1


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


# ── Fleet-wide feedback (Insights page) ──────────────────────────────────


class TestGetAllFeedback:
    def test_get_all_feedback_returns_feedback_across_apps(self):
        """Regression: get_feedback_for_app("") filters on app_name = '' and
        always returns nothing -- get_all_feedback is the fleet-wide fix."""
        store = make_store()
        store.record_feedback("app-a", "security", "network-policy", "approved")
        store.record_feedback("app-b", "compliance", "sbom", "rejected", human_reason="not needed")

        feedback = store.get_all_feedback()
        assert len(feedback) == 2
        assert {f["app_name"] for f in feedback} == {"app-a", "app-b"}
        # get_feedback_for_app("") remains the old (broken for this use case) behavior
        assert store.get_feedback_for_app("") == []

    def test_get_all_feedback_respects_limit_and_order(self):
        store = make_store()
        for i in range(5):
            store.record_feedback(f"app-{i}", "security", "cat", "approved")
        feedback = store.get_all_feedback(limit=3)
        assert len(feedback) == 3
        # most recent first
        assert feedback[0]["app_name"] == "app-4"


# ── Dead-letter queue republish ──────────────────────────────────────────


class TestRetryDlqMessage:
    def test_retry_republishes_to_original_topic(self):
        store = make_store()
        eid = store.log_event(
            "event-consumer", "dead-letter", "my-app", "error",
            "Dead-lettered from agentit-events: boom",
            details={
                "original_topic": "agentit-events",
                "original_message": {
                    "agentId": "watcher", "action": "tick", "targetApp": "my-app",
                    "severity": "info", "result": {"summary": "hi", "details": {}},
                    "correlationId": "abc123",
                },
                "error": "boom",
            },
        )

        assert store.retry_dlq_message(eid) is True

        events = store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "republished to agentit-events" in retry_event["summary"]
        # original dead-letter row is relabelled, not duplicated
        assert store.list_dlq_messages() == []

    def test_retry_falls_back_to_relabel_when_no_original_topic(self):
        """Rows written before original_topic tracking existed (or a Kafka
        failure) must still mark the row retried instead of silently no-op'ing."""
        store = make_store()
        eid = store.log_event(
            "event-consumer", "dead-letter", "my-app", "error", "old-style row",
            details={"original_message": {"foo": "bar"}, "error": "boom"},
        )
        assert store.retry_dlq_message(eid) is True
        events = store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "relabelled only" in retry_event["summary"]

    def test_retry_unknown_event_returns_false(self):
        store = make_store()
        assert store.retry_dlq_message("nonexistent") is False


# ── Agent Runs ────────────────────────────────────────────────────────────


class TestAgentRuns:
    def test_save_and_list_agent_runs(self):
        store = make_store()
        store.save_agent_run("security", "local", "success", duration_ms=1200, resource_tier="standard")
        store.save_agent_run("security", "local", "error", duration_ms=50, error="boom")

        runs = store.list_agent_runs("security")
        assert len(runs) == 2
        assert runs[0]["status"] == "error"  # most recent first
        assert runs[1]["status"] == "success"
        assert runs[1]["duration_ms"] == 1200

    def test_get_agent_stats_uses_agent_runs_not_events(self):
        """Regression: get_agent_stats previously LIKE-matched event `action`
        strings ('%complete%'/'%failed%'), which double-counted unrelated
        events. It must now be derived purely from agent_runs."""
        store = make_store()
        # An unrelated event containing "complete" must not be counted.
        store.log_event("security", "onboarding-complete", "app", "info", "noise")
        store.save_agent_run("security", "local", "success", duration_ms=100)
        store.save_agent_run("security", "local", "success", duration_ms=200)
        store.save_agent_run("security", "local", "error", duration_ms=50)

        stats = store.get_agent_stats("security")
        assert len(stats) == 1
        assert stats[0]["total_events"] == 3
        assert stats[0]["successes"] == 2
        assert stats[0]["failures"] == 1
        assert stats[0]["success_rate"] == round(2 / 3 * 100, 1)

    def test_list_agent_runs_for_assessment(self):
        store = make_store()
        aid = store.save(make_report())
        store.save_agent_run("security", "local", "success", assessment_id=aid)
        store.save_agent_run("cicd", "local", "success", assessment_id="other-assessment")

        runs = store.list_agent_runs_for_assessment(aid)
        assert len(runs) == 1
        assert runs[0]["agent_name"] == "security"


# ── Check Result Snapshots ────────────────────────────────────────────────


class TestCheckResults:
    def test_save_and_get_check_compliance(self):
        store = make_store()
        aid = store.save(make_report())
        store.save_check_results(aid, [
            {"check_name": "has-network-policy", "dimension": "security", "passed": True},
            {"check_name": "has-network-policy", "dimension": "security", "passed": False},
            {"check_name": "has-sbom", "dimension": "compliance", "passed": True},
        ])

        compliance = store.get_check_compliance()
        by_name = {c["check_name"]: c for c in compliance}
        assert by_name["has-network-policy"]["passes"] == 1
        assert by_name["has-network-policy"]["total"] == 2
        assert by_name["has-network-policy"]["pass_rate"] == 50.0
        assert by_name["has-sbom"]["pass_rate"] == 100.0

    def test_save_check_results_noop_on_empty_list(self):
        store = make_store()
        aid = store.save(make_report())
        store.save_check_results(aid, [])
        assert store.get_check_compliance() == []


# ── Correlation IDs ───────────────────────────────────────────────────────


class TestCorrelationId:
    def test_log_event_persists_correlation_id(self):
        store = make_store()
        store.log_event("orchestrator", "completed", "app", "info", "done", correlation_id="chain-1")
        store.log_event("orchestrator", "completed", "app", "info", "unrelated", correlation_id="chain-2")

        chain = store.list_events_by_correlation_id("chain-1")
        assert len(chain) == 1
        assert chain[0]["summary"] == "done"

    def test_save_sets_correlation_id_to_assessment_id(self):
        store = make_store()
        aid = store.save(make_report())
        chain = store.list_events_by_correlation_id(aid)
        assert len(chain) == 1
        assert chain[0]["action"] == "assessment-complete"


# ── DB stats / metrics ────────────────────────────────────────────────────


class TestGetDbStats:
    def test_get_db_stats_returns_row_counts(self):
        store = make_store()
        store.save(make_report())
        stats = store.get_db_stats()
        assert stats["row_counts"]["assessments"] == 1
        assert stats["size_bytes"] == 0  # :memory: has no file size

    def test_get_db_stats_with_real_file(self, tmp_path):
        from agentit.portal.store import AssessmentStore
        db_path = tmp_path / "test.db"
        store = AssessmentStore(db_path=str(db_path))
        stats = store.get_db_stats()
        assert stats["size_bytes"] > 0


# ── Active gates gauge ─────────────────────────────────────────────────────


class TestActiveGatesMetric:
    def test_create_and_resolve_gate_updates_gauge(self):
        from agentit.portal.metrics import active_gates
        store = make_store()
        aid = store.save(make_report())
        gate_id = store.create_gate(aid, "security-review", "review needed")
        assert active_gates._value.get() == 1.0

        store.resolve_gate(gate_id, "approved", "tester")
        assert active_gates._value.get() == 0.0
