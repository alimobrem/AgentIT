"""Integration tests for the async Postgres store (Phase 2 of
docs/postgres-migration-plan.md).

Requires a real Postgres instance — there is no async in-memory Postgres
equivalent (see plan §8). Skipped by default; opt in with
``--run-postgres-tests`` plus a reachable ``AGENTIT_TEST_PG_DSN`` (or a local
``podman``/``docker`` on PATH, in which case a throw-away container is
started and torn down automatically for the test session).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone

import pytest

from agentit.models import (
    ArchitectureInfo,
    AssessmentReport,
    DimensionScore,
    Finding,
    Language,
    Severity,
    StackInfo,
)
from agentit.portal.store_pg import AssessmentStore

pytestmark = pytest.mark.postgres

_CONTAINER_NAME = f"agentit-pg-test-{uuid.uuid4().hex[:8]}"


def _container_runtime() -> str | None:
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def _make_report(repo_name: str = "test-app") -> AssessmentReport:
    return AssessmentReport(
        repo_url=f"https://github.com/org/{repo_name}",
        repo_name=repo_name,
        assessed_at=datetime.now(timezone.utc),
        stack=StackInfo(
            languages=[Language(name="python", file_count=10, percentage=100.0)],
            frameworks=[], databases=[], runtimes=[], package_managers=[],
        ),
        architecture=ArchitectureInfo(
            service_count=1, architecture_style="monolith", has_api=True,
            api_style="REST", external_dependencies=[],
        ),
        scores=[DimensionScore(
            dimension="security", score=80, max_score=100,
            findings=[Finding(category="test", severity=Severity.critical,
                               description="minor", recommendation="fix")],
        )],
        criticality="medium",
        summary="test summary",
        remediation_plan=[],
    )


@pytest.fixture(scope="session")
def postgres_dsn():
    """Session-scoped DSN, either from AGENTIT_TEST_PG_DSN or a throw-away
    container started via podman/docker."""
    env_dsn = os.environ.get("AGENTIT_TEST_PG_DSN")
    if env_dsn:
        yield env_dsn
        return

    runtime = _container_runtime()
    if runtime is None:
        pytest.skip("no AGENTIT_TEST_PG_DSN and no podman/docker on PATH")

    port = 55433
    subprocess.run([runtime, "rm", "-f", _CONTAINER_NAME], capture_output=True)
    result = subprocess.run(
        [
            runtime, "run", "-d", "--name", _CONTAINER_NAME,
            "-e", "POSTGRES_USER=agentit_test",
            "-e", "POSTGRES_PASSWORD=agentit_test",
            "-e", "POSTGRES_DB=agentit_test",
            "-p", f"{port}:5432",
            "postgres:16-alpine",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not start Postgres via {runtime}: {result.stderr.strip()}")

    dsn = f"postgresql://agentit_test:agentit_test@localhost:{port}/agentit_test"
    try:
        for _ in range(30):
            check = subprocess.run(
                [runtime, "exec", _CONTAINER_NAME, "pg_isready", "-U", "agentit_test"],
                capture_output=True,
            )
            if check.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.skip("Postgres container did not become ready in time")
        yield dsn
    finally:
        subprocess.run([runtime, "rm", "-f", _CONTAINER_NAME], capture_output=True)


@pytest.fixture
async def store(postgres_dsn):
    """Fresh AssessmentStore per test, with all tables truncated first for
    isolation (mirrors the ':memory:' isolation the SQLite fixture gets for
    free — see plan §8)."""
    s = await AssessmentStore.create(postgres_dsn, min_size=1, max_size=3)
    async with s._pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE assessments, onboarding_results, events, gates, remediations, "
            "agent_registry, slos, apply_results, settings, remediation_jobs, "
            "scheduled_operations, processed_webhooks, agent_feedback, "
            "skill_effectiveness, suppressed_checks, skill_inventory_snapshots, "
            "agent_runs, check_results CASCADE"
        )
    yield s
    await s.close()


class TestSettings:
    async def test_set_and_get_setting(self, store):
        await store.set_setting("theme", "dark")
        assert await store.get_setting("theme") == "dark"

    async def test_set_setting_overwrites(self, store):
        await store.set_setting("theme", "dark")
        await store.set_setting("theme", "light")
        assert await store.get_setting("theme") == "light"

    async def test_get_missing_setting(self, store):
        assert await store.get_setting("nope") is None

    async def test_list_settings(self, store):
        await store.set_setting("a", "1")
        await store.set_setting("b", "2")
        rows = await store.list_settings()
        assert [r["key"] for r in rows] == ["a", "b"]
        # Shape parity check: updated_at comes back as an ISO string, not a
        # datetime object (see module docstring in store_pg.py).
        assert isinstance(rows[0]["updated_at"], str)


class TestAssessments:
    async def test_save_and_get_roundtrip(self, store):
        report = _make_report()
        assessment_id = await store.save(report)
        fetched = await store.get(assessment_id)
        assert fetched is not None
        assert fetched.repo_name == "test-app"
        assert fetched.overall_score == report.overall_score

    async def test_get_missing_returns_none(self, store):
        assert await store.get(uuid.uuid4().hex) is None

    async def test_save_logs_event(self, store):
        assessment_id = await store.save(_make_report())
        events = await store.list_events()
        assert any(e["action"] == "assessment-complete" for e in events)

    async def test_list_all_orders_desc(self, store):
        await store.save(_make_report("older"))
        await store.save(_make_report("newer"))
        rows = await store.list_all()
        assert len(rows) == 2
        assert isinstance(rows[0]["assessed_at"], str)

    async def test_delete_cascades(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_onboarding(assessment_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])
        assert await store.delete(assessment_id) is True
        assert await store.get(assessment_id) is None
        assert await store.get_onboarding(assessment_id) is None

    async def test_delete_missing_returns_false(self, store):
        assert await store.delete(uuid.uuid4().hex) is False


class TestFleetAndTrend:
    async def test_get_fleet_data(self, store):
        await store.save(_make_report("app-a"))
        fleet = await store.get_fleet_data()
        assert len(fleet) == 1
        assert fleet[0]["repo_name"] == "app-a"
        assert fleet[0]["critical_count"] == 1
        assert isinstance(fleet[0]["last_assessed"], str)

    async def test_get_trend_no_history(self, store):
        trend = await store.get_trend("https://github.com/org/nope")
        assert trend == {
            "current_score": None, "previous_score": None,
            "delta": None, "assessments_count": 0,
        }


class TestGates:
    async def test_create_gate_dedupes_pending(self, store):
        assessment_id = await store.save(_make_report())
        id1 = await store.create_gate(assessment_id, "security", "needs review")
        id2 = await store.create_gate(assessment_id, "security", "needs review")
        assert id1 == id2

    async def test_resolve_gate(self, store):
        assessment_id = await store.save(_make_report())
        gate_id = await store.create_gate(assessment_id, "security", "needs review")
        assert await store.resolve_gate(gate_id, "approved", "alice") is True
        gates = await store.list_all_gates()
        assert gates[0]["status"] == "approved"
        assert gates[0]["resolved_by"] == "alice"

    async def test_resolve_gate_twice_fails(self, store):
        assessment_id = await store.save(_make_report())
        gate_id = await store.create_gate(assessment_id, "security", "needs review")
        await store.resolve_gate(gate_id, "approved", "alice")
        assert await store.resolve_gate(gate_id, "rejected", "bob") is False


class TestSlos:
    async def test_save_list_update_delete(self, store):
        assessment_id = await store.save(_make_report())
        slo_id = await store.save_slo(assessment_id, "availability", 99.9)
        slos = await store.list_slos(assessment_id)
        assert len(slos) == 1
        assert slos[0]["id"] == slo_id

        assert await store.update_slo(slo_id, 99.95, "met") is True
        slos = await store.list_slos(assessment_id)
        assert slos[0]["status"] == "met"

        assert await store.delete_slo(slo_id, assessment_id) is True
        assert await store.list_slos(assessment_id) == []

    async def test_delete_slo_wrong_assessment_returns_false(self, store):
        assessment_id = await store.save(_make_report())
        other_id = await store.save(_make_report("other-app"))
        slo_id = await store.save_slo(assessment_id, "availability", 99.9)
        assert await store.delete_slo(slo_id, other_id) is False


class TestRemediations:
    async def test_save_list_complete_delete(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_remediation(assessment_id, "hardening", "Add NetworkPolicy")
        remediations = await store.list_remediations(assessment_id)
        assert len(remediations) == 1
        rem_id = remediations[0]["id"]

        assert await store.complete_remediation(rem_id) is True
        remediations = await store.list_remediations(assessment_id)
        assert remediations[0]["status"] == "completed"

        assert await store.delete_remediation(rem_id, assessment_id) is True
        assert await store.list_remediations(assessment_id) == []

    async def test_delete_remediation_wrong_assessment_returns_false(self, store):
        assessment_id = await store.save(_make_report())
        other_id = await store.save(_make_report("other-app"))
        await store.save_remediation(assessment_id, "hardening", "Add NetworkPolicy")
        rem_id = (await store.list_remediations(assessment_id))[0]["id"]
        assert await store.delete_remediation(rem_id, other_id) is False


class TestAgentRegistry:
    async def test_register_agent_is_idempotent_on_name(self, store):
        await store.register_agent("scanner", "security", capabilities='["scan"]')
        await store.register_agent("scanner", "security", capabilities='["scan", "fix"]')
        agents = await store.list_agents()
        assert len(agents) == 1
        assert agents[0]["capabilities"] == '["scan", "fix"]'

    async def test_register_agent_accepts_prose_capabilities(self, store):
        """Regression guard for a real bug found during the live Postgres
        cutover: AGENT_CAPABILITIES (agents/capabilities.py) passes a
        human-readable prose description, never actual JSON -- e.g.
        "VPA, cost labels, cost report" -- and the column must accept it
        as plain TEXT (matching store.py), not reject it via a `::jsonb`
        cast."""
        await store.register_agent("cost", "cost", capabilities="VPA, cost labels, cost report")
        agents = await store.list_agents()
        agent = next(a for a in agents if a["agent_name"] == "cost")
        assert agent["capabilities"] == "VPA, cost labels, cost report"

    async def test_agent_heartbeat(self, store):
        await store.register_agent("scanner", "security")
        assert await store.agent_heartbeat("scanner") is True

    async def test_agent_heartbeat_upserts_unregistered_agent(self, store):
        """Long-lived watchers (vuln-watcher, slo-tracker, ...) never call
        register_agent -- agent_heartbeat must create the row itself so their
        "last seen" actually shows up on the Agents/Schedules pages."""
        assert await store.agent_heartbeat("vuln-watcher") is True
        agents = await store.list_agents()
        assert any(a["agent_name"] == "vuln-watcher" for a in agents)
        watcher = next(a for a in agents if a["agent_name"] == "vuln-watcher")
        assert watcher["category"] == "watcher"
        assert watcher["last_heartbeat"] is not None

    async def test_agent_heartbeat_custom_category(self, store):
        await store.agent_heartbeat("cve-scanner", category="security")
        agents = await store.list_agents()
        watcher = next(a for a in agents if a["agent_name"] == "cve-scanner")
        assert watcher["category"] == "security"

    async def test_prune_stale_agents_removes_unknown_names(self, store):
        await store.register_agent("security", "hardening")
        await store.register_agent("cost", "cost")

        pruned = await store.prune_stale_agents(known_names={"cost"})

        assert pruned == ["security"]
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"cost"}

    async def test_prune_stale_agents_preserves_known_names(self, store):
        known = {"cost", "dependency", "codechange", "vuln-watcher"}
        for name in known:
            await store.register_agent(name, "test")

        pruned = await store.prune_stale_agents(known_names=known)

        assert pruned == []
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == known


class TestApplyResultsJsonRoundtrip:
    async def test_save_and_get_apply_results(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_apply_results(
            assessment_id,
            {"applied": ["a.yaml"], "skipped": [], "errors": [], "repo_files": ["b.yaml"]},
            namespace="agentit",
            dry_run=False,
        )
        result = await store.get_apply_results(assessment_id)
        assert result["applied"] == ["a.yaml"]
        assert result["repo_files"] == ["b.yaml"]
        assert result["dry_run"] is False


class TestScheduledOperations:
    async def test_toggle_schedule(self, store):
        schedule_id = await store.create_schedule("app", "job", "agent", "* * * * *", "cmd")
        assert await store.toggle_schedule(schedule_id, False) is True
        schedules = await store.list_schedules()
        assert schedules[0]["enabled"] is False

    async def test_has_schedules_for_app(self, store):
        assert await store.has_schedules_for_app("app") is False
        await store.create_schedule("app", "job", "agent", "* * * * *", "cmd")
        assert await store.has_schedules_for_app("app") is True


class TestWebhookDedup:
    async def test_mark_and_check_processed(self, store):
        assert await store.webhook_already_processed("delivery-1") is False
        await store.mark_webhook_processed("delivery-1")
        assert await store.webhook_already_processed("delivery-1") is True
        # ON CONFLICT DO NOTHING must not raise on a repeat delivery id.
        await store.mark_webhook_processed("delivery-1")


class TestSuppressedChecks:
    async def test_suppress_and_unsuppress(self, store):
        await store.suppress_check("app", "check-a", reason="flaky")
        assert await store.get_suppressed_sources("app") == {"check-a"}
        await store.suppress_check("app", "check-a", reason="still flaky")
        suppressions = await store.get_suppressions("app")
        assert len(suppressions) == 1
        assert suppressions[0]["reason"] == "still flaky"
        await store.unsuppress_check("app", "check-a")
        assert await store.get_suppressed_sources("app") == set()


class TestSkillInventorySnapshots:
    async def test_save_and_get_last_snapshot(self, store):
        assert await store.get_last_skill_inventory_snapshot() is None
        await store.save_skill_inventory_snapshot({"skills": ["a"]})
        await store.save_skill_inventory_snapshot({"skills": ["a", "b"]})
        latest = await store.get_last_skill_inventory_snapshot()
        assert latest["skills"] == ["a", "b"]


class TestPurgeOldData:
    async def test_purge_old_data_no_op_on_fresh_data(self, store):
        await store.save(_make_report())
        counts = await store.purge_old_data(retention_days=30)
        assert all(c == 0 for c in counts.values())

    async def test_purge_deletes_old_events_but_keeps_latest_onboarding(self, store):
        from datetime import timedelta

        assessment_id = await store.save(_make_report())
        old_onboarding_id = await store.save_onboarding(assessment_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])
        new_onboarding_id = await store.save_onboarding(assessment_id, [{"category": "y", "path": "b.yaml", "content": "", "description": ""}])

        old_cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        async with store._pool.acquire() as conn:
            await conn.execute("UPDATE events SET timestamp = $1", old_cutoff)
            await conn.execute("UPDATE onboarding_results SET created_at = $1 WHERE id = $2", old_cutoff, old_onboarding_id)

        counts = await store.purge_old_data(retention_days=30)
        assert counts["events"] >= 1
        assert counts["onboarding_results"] == 1

        remaining_ids = {r["id"] for r in await store.list_onboardings(assessment_id)}
        assert remaining_ids == {new_onboarding_id}


class TestExportAll:
    async def test_export_all_covers_every_table(self, store):
        await store.save(_make_report())
        data = await store.export_all()
        assert "assessments" in data
        assert len(data["assessments"]) == 1
        # All 18 tables (16 from the plan's original §4 list, plus the
        # self-observability agent_runs/check_results tables) must be
        # represented.
        from agentit.portal.store_pg import _ALL_TABLES
        assert set(data.keys()) == set(_ALL_TABLES)


class TestEventsCorrelationAndAction:
    async def test_log_event_with_correlation_id(self, store):
        await store.log_event("agent", "step-1", "app", "info", "first", correlation_id="chain-1")
        await store.log_event("agent", "step-2", "app", "info", "second", correlation_id="chain-1")
        await store.log_event("agent", "step-3", "app", "info", "unrelated", correlation_id="chain-2")

        chain = await store.list_events_by_correlation_id("chain-1")
        assert [e["action"] for e in chain] == ["step-1", "step-2"]

    async def test_save_populates_correlation_id(self, store):
        """save() must pass its new assessment_id as the correlation_id,
        matching store.py, so an assess -> onboard -> apply chain is
        traceable end to end."""
        assessment_id = await store.save(_make_report())
        chain = await store.list_events_by_correlation_id(assessment_id)
        assert any(e["action"] == "assessment-complete" for e in chain)

    async def test_save_onboarding_populates_correlation_id(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_onboarding(assessment_id, [{"category": "x", "path": "a.yaml", "content": "", "description": ""}])
        chain = await store.list_events_by_correlation_id(assessment_id)
        assert any(e["action"] == "onboarding-complete" for e in chain)

    async def test_list_events_by_action(self, store):
        await store.log_event("agent-a", "decision", "app", "info", "a")
        await store.log_event("agent-b", "decision", "app", "info", "b")
        await store.log_event("agent-c", "other-action", "app", "info", "c")
        rows = await store.list_events_by_action("decision")
        assert len(rows) == 2
        assert {r["agent_id"] for r in rows} == {"agent-a", "agent-b"}


class TestGetEvent:
    """get_event() -- single-row lookup by primary key, backing the
    Self-Improvement tab's per-run drill-through page."""

    async def test_returns_the_matching_event(self, store):
        eid = await store.log_event("capability-scout", "capability-run", None, "info", "proposed something")
        event = await store.get_event(eid)
        assert event is not None
        assert event["id"] == eid
        assert event["summary"] == "proposed something"

    async def test_returns_none_for_unknown_id(self, store):
        assert await store.get_event("does-not-exist") is None


class TestRetryDlqMessage:
    async def test_retry_republishes_to_original_topic(self, store):
        eid = await store.log_event(
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

        assert await store.retry_dlq_message(eid) is True

        events = await store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "republished to agentit-events" in retry_event["summary"]
        # original dead-letter row is relabelled, not duplicated
        assert await store.list_dlq_messages() == []

    async def test_retry_falls_back_to_relabel_when_no_original_topic(self, store):
        """Rows written before original_topic tracking existed (or a Kafka
        failure) must still mark the row retried instead of silently no-op'ing."""
        eid = await store.log_event(
            "event-consumer", "dead-letter", "my-app", "error", "old-style row",
            details={"original_message": {"foo": "bar"}, "error": "boom"},
        )
        assert await store.retry_dlq_message(eid) is True
        events = await store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "relabelled only" in retry_event["summary"]

    async def test_retry_unknown_event_returns_false(self, store):
        assert await store.retry_dlq_message("nonexistent") is False


class TestAgentRuns:
    async def test_save_and_list_agent_runs(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_agent_run(
            "hardening", "local", "success", assessment_id=assessment_id, duration_ms=120,
        )
        await store.save_agent_run(
            "hardening", "local", "failed", assessment_id=assessment_id, error="boom",
        )

        runs = await store.list_agent_runs("hardening")
        assert len(runs) == 2
        assert runs[0]["status"] == "failed"  # most recent first

        by_assessment = await store.list_agent_runs_for_assessment(assessment_id)
        assert [r["status"] for r in by_assessment] == ["success", "failed"]  # ascending


class TestCheckResults:
    async def test_save_and_get_check_compliance(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_check_results(assessment_id, [
            {"check_name": "health-check", "dimension": "observability", "passed": True},
            {"check_name": "health-check", "dimension": "observability", "passed": False},
        ])
        compliance = await store.get_check_compliance()
        row = next(r for r in compliance if r["check_name"] == "health-check")
        assert row["total"] == 2
        assert row["passes"] == 1
        assert row["pass_rate"] == 50.0

    async def test_save_check_results_no_op_on_empty_list(self, store):
        assessment_id = await store.save(_make_report())
        await store.save_check_results(assessment_id, [])
        assert await store.get_check_compliance() == []


class TestGetAllFeedback:
    async def test_get_all_feedback_across_apps(self, store):
        await store.record_feedback("app-a", "hardening", "security", "approved")
        await store.record_feedback("app-b", "hardening", "security", "rejected")
        feedback = await store.get_all_feedback()
        assert len(feedback) == 2
        assert {f["app_name"] for f in feedback} == {"app-a", "app-b"}


class TestFleetWideRejectionStats:
    """get_fleet_wide_rejection_stats() -- capability-scout's fleet-wide
    aggregate (docs/self-improvement-for-agentit.md)."""

    async def test_aggregates_rejection_rate_per_category_across_apps(self, store):
        await store.record_feedback("app-a", "hardening", "network-policy", "rejected")
        await store.record_feedback("app-b", "hardening", "network-policy", "rejected")
        await store.record_feedback("app-c", "hardening", "network-policy", "approved")

        stats = await store.get_fleet_wide_rejection_stats()
        by_category = {s["finding_category"]: s for s in stats}
        assert by_category["network-policy"]["total"] == 3
        assert by_category["network-policy"]["rejected"] == 2

    async def test_empty_when_no_feedback(self, store):
        assert await store.get_fleet_wide_rejection_stats() == []


class TestDbStats:
    async def test_get_db_stats_row_counts_and_size(self, store):
        await store.save(_make_report())
        stats = await store.get_db_stats()
        assert stats["row_counts"]["assessments"] == 1
        assert stats["size_bytes"] > 0
