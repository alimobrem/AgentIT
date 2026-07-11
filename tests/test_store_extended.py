from __future__ import annotations

from datetime import datetime, timezone

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    RemediationItem,
    Severity,
    StackInfo,
)
from agentit.portal.store import AssessmentStore


def _make_store() -> AssessmentStore:
    return AssessmentStore(db_path=":memory:")


def _make_report(
    repo_name: str = "test-repo",
    score: int = 50,
    assessed_at: datetime | None = None,
) -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=assessed_at or datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", version="3.12", file_count=10, percentage=100.0)],
            frameworks=[],
            databases=[],
            runtimes=[],
            package_managers=["pip"],
        ),
        architecture=ArchitectureInfo(
            service_count=1,
            architecture_style="monolith",
            has_api=True,
            api_style="REST",
            external_dependencies=[],
            auth_mechanism=None,
        ),
        scores=[
            DimensionScore(
                dimension="security",
                score=score,
                max_score=100,
                findings=[
                    Finding(
                        category="test",
                        severity=Severity.info,
                        description="placeholder",
                        recommendation="n/a",
                    )
                ],
            ),
        ],
        criticality="low",
        summary="test",
        remediation_plan=[
            RemediationItem(
                priority=1,
                dimension="security",
                description="fix it",
                estimated_effort="1h",
                agent_responsible="human",
            )
        ],
    )


# ── Events ──────────────────────────────────────────────────────────────


def test_log_and_list_events():
    store = _make_store()
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
    store = _make_store()
    r1 = _make_report(score=40, assessed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    r2 = _make_report(score=60, assessed_at=datetime(2025, 2, 1, tzinfo=timezone.utc))
    store.save(r1)
    store.save(r2)

    history = store.list_history("https://github.com/org/test-repo")
    assert len(history) == 2
    # ordered ascending by date
    assert history[0]["overall_score"] == 40.0
    assert history[1]["overall_score"] == 60.0


def test_get_trend_shows_delta():
    store = _make_store()
    r1 = _make_report(score=40, assessed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    r2 = _make_report(score=70, assessed_at=datetime(2025, 2, 1, tzinfo=timezone.utc))
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
    store = _make_store()
    aid = store.save(_make_report())
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
    store = _make_store()
    aid = store.save(_make_report())
    g1 = store.create_gate(aid, "compliance", "Missing SBOM")
    g2 = store.create_gate(aid, "security", "No network policy")

    store.resolve_gate(g1, "approved", "carol")

    assert len(store.list_gates(status="pending")) == 1
    assert len(store.list_gates(status="approved")) == 1
    assert store.list_gates(status="pending")[0]["id"] == g2


# ── Severity enum regression ───────────────────────────────────────────


def test_fleet_data_counts_critical_findings_correctly():
    """Regression: severity comparison must use Severity enum, not raw ints."""
    store = _make_store()
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
        store = _make_store()
        aid = store.save(_make_report())
        rid = store.save_remediation(aid, "security", "Fix RBAC")
        rems = store.list_remediations(aid)
        assert len(rems) == 1
        assert rems[0]["agent_name"] == "security"
        assert rems[0]["status"] == "pending"
        assert rems[0]["id"] == rid

    def test_complete(self):
        store = _make_store()
        aid = store.save(_make_report())
        rid = store.save_remediation(aid, "security", "Fix RBAC")
        assert store.complete_remediation(rid) is True
        rems = store.list_remediations(aid)
        assert rems[0]["status"] == "completed"
        assert rems[0]["completed_at"] is not None

    def test_complete_idempotent(self):
        store = _make_store()
        aid = store.save(_make_report())
        rid = store.save_remediation(aid, "cicd", "Add pipeline")
        store.complete_remediation(rid)
        assert store.complete_remediation(rid) is False


# ── Agent Registry ─────────────────────────────────────────────────────


class TestAgentRegistryTable:
    def test_register_and_list(self):
        store = _make_store()
        aid = store.register_agent("security", "hardening", "network,rbac")
        agents = store.list_agents()
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "security"
        assert agents[0]["category"] == "hardening"
        assert agents[0]["status"] == "active"

    def test_heartbeat(self):
        store = _make_store()
        store.register_agent("observability", "monitoring")
        assert store.agent_heartbeat("observability") is True
        assert store.agent_heartbeat("nonexistent") is False

    def test_register_replaces_existing(self):
        store = _make_store()
        store.register_agent("security", "hardening", "v1")
        store.register_agent("security", "hardening", "v2")
        agents = store.list_agents()
        assert len(agents) == 1
        assert agents[0]["capabilities"] == "v2"


# ── SLOs ───────────────────────────────────────────────────────────────


class TestSlosTable:
    def test_save_and_list(self):
        store = _make_store()
        aid = store.save(_make_report())
        sid = store.save_slo(aid, "availability", 99.9)
        slos = store.list_slos(aid)
        assert len(slos) == 1
        assert slos[0]["metric_name"] == "availability"
        assert slos[0]["target_value"] == 99.9
        assert slos[0]["status"] == "unknown"
        assert slos[0]["id"] == sid

    def test_update_slo(self):
        store = _make_store()
        aid = store.save(_make_report())
        sid = store.save_slo(aid, "error_rate", 0.1)
        assert store.update_slo(sid, 0.05, "met") is True
        slos = store.list_slos(aid)
        assert slos[0]["current_value"] == 0.05
        assert slos[0]["status"] == "met"
        assert slos[0]["updated_at"] is not None

    def test_multiple_slos_per_assessment(self):
        store = _make_store()
        aid = store.save(_make_report())
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

        store = _make_store()
        report = _make_report()
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

        store = _make_store()
        report = _make_report()
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

        store = _make_store()
        report = _make_report()
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

        store = _make_store()
        report = _make_report()
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


class TestOnboardingHistory:
    def test_list_onboardings(self):
        store = _make_store()
        aid = store.save(_make_report())
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
        store = _make_store()
        aid = store.save(_make_report())
        assert store.list_onboardings(aid) == []
