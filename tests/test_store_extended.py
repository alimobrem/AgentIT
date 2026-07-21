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


async def test_log_and_list_events():
    store = await make_store()
    eid = await store.log_event("bot", "deploy", "my-app", "info", "deployed v1")
    assert eid

    events = await store.list_events()
    assert len(events) == 1
    assert events[0]["agent_id"] == "bot"
    assert events[0]["action"] == "deploy"
    assert events[0]["target_app"] == "my-app"
    assert events[0]["summary"] == "deployed v1"

    # filter by target_app
    await store.log_event("bot", "scan", "other-app", "warning", "drift detected")
    filtered = await store.list_events(target_app="other-app")
    assert len(filtered) == 1
    assert filtered[0]["target_app"] == "other-app"

    # all events
    assert len(await store.list_events()) == 2


# ── Assessment history ──────────────────────────────────────────────────


async def test_list_history_returns_multiple_assessments():
    store = await make_store()
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
    await store.save(r1)
    await store.save(r2)

    history = await store.list_history("https://github.com/org/test-repo")
    assert len(history) == 2
    # ordered ascending by date
    assert history[0]["overall_score"] == 40.0
    assert history[1]["overall_score"] == 60.0


async def test_get_trend_shows_delta():
    store = await make_store()
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
    await store.save(r1)
    await store.save(r2)

    trend = await store.get_trend("https://github.com/org/test-repo")
    assert trend["current_score"] == 70.0
    assert trend["previous_score"] == 40.0
    assert trend["delta"] == 30.0
    assert trend["assessments_count"] == 2

    # empty repo
    empty = await store.get_trend("https://github.com/org/nonexistent")
    assert empty["assessments_count"] == 0
    assert empty["delta"] is None


async def test_save_carries_forward_infra_repo_url_from_prior_assessment():
    """A re-assessment (``save()``) of an already-GitOps-registered app
    must not lose ``infra_repo_url`` just because the new report didn't
    explicitly set it -- see delivery.py's "registered but no infra_repo_url
    is known" refusal path this closes."""
    store = await make_store()
    r1 = make_report(repo_name="pinky")
    r1.assessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    aid1 = await store.save(r1)
    await store.set_infra_repo_url(aid1, "https://github.com/org/infra")

    r2 = make_report(repo_name="pinky")  # fresh report, infra_repo_url unset
    r2.assessed_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    assert r2.infra_repo_url is None
    aid2 = await store.save(r2)

    saved = await store.get(aid2)
    assert saved.infra_repo_url == "https://github.com/org/infra"


async def test_save_does_not_override_explicit_infra_repo_url():
    """If the new report already carries its own ``infra_repo_url`` (e.g.
    supplied via the assess form), that explicit value wins over whatever
    an earlier assessment had -- carry-forward only fills in a gap, it
    never overwrites a value the caller actually provided."""
    store = await make_store()
    r1 = make_report(repo_name="pinky")
    r1.assessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    aid1 = await store.save(r1)
    await store.set_infra_repo_url(aid1, "https://github.com/org/old-infra")

    r2 = make_report(repo_name="pinky")
    r2.assessed_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    r2.infra_repo_url = "https://github.com/org/new-infra"
    aid2 = await store.save(r2)

    saved = await store.get(aid2)
    assert saved.infra_repo_url == "https://github.com/org/new-infra"


async def test_save_leaves_infra_repo_url_none_when_never_set():
    """A brand-new app with no registration history at all still gets
    ``infra_repo_url is None`` -- carry-forward has nothing to find, and
    must not fabricate a value."""
    store = await make_store()
    r1 = make_report(repo_name="never-registered")
    aid1 = await store.save(r1)

    saved = await store.get(aid1)
    assert saved.infra_repo_url is None


# ── apps table (app-vs-assessment data model) ──────────────────────────


class TestAppsTable:
    """The `apps` table is the single, always-current source of app-level
    facts (currently just `infra_repo_url`) that used to be re-derived by
    scanning `assessments.report_json` history on every `save()` -- see
    docs/architecture.md's "Data model: assessments vs. apps" section.
    """

    async def test_save_creates_an_apps_row(self):
        store = await make_store()
        await store.save(make_report(repo_name="new-app", repo_url="https://github.com/org/new-app"))
        row = await store._pool.fetchrow(
            "SELECT * FROM apps WHERE repo_url = $1", "https://github.com/org/new-app",
        )
        assert row is not None
        assert row["repo_name"] == "new-app"
        assert row["infra_repo_url"] is None

    async def test_set_infra_repo_url_updates_apps_row_even_for_a_non_latest_assessment(self):
        """Closes a gap the pre-`apps`-table fix still had: registering
        against an assessment_id that ISN'T the app's latest one (e.g. a
        stale browser tab pointed at an older Assessment Detail page) must
        still make the app show up as registered on `get_fleet_data()`,
        which only ever looks at the latest assessment row -- not just on
        the specific (now-stale) assessment that was registered.
        """
        store = await make_store()
        old_report = make_report(repo_name="stale-tab-app")
        old_report.assessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        old_id = await store.save(old_report)

        new_report = make_report(repo_name="stale-tab-app")
        new_report.assessed_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
        new_id = await store.save(new_report)
        assert new_id != old_id

        # Register against the OLD (no longer latest) assessment_id.
        assert await store.set_infra_repo_url(old_id, "https://github.com/org/infra") is True

        fleet_row = next(
            r for r in await store.get_fleet_data() if r["repo_url"] == "https://github.com/org/stale-tab-app"
        )
        assert fleet_row["id"] == new_id  # still keyed to the latest assessment
        assert fleet_row["infra_repo_url"] == "https://github.com/org/infra"  # but registration is visible

    async def test_apps_table_backfilled_from_existing_assessments_on_reopen(self, postgres_dsn):
        """A database that predates the `apps` table (assessments + a
        registration already recorded in `report_json`, but no `apps` row
        yet) must get that history backfilled the next time `create()` runs
        SCHEMA_SQL against it (e.g. a second replica/pod connecting to the
        same instance) -- using the same "most recent non-null value wins"
        logic `_last_known_infra_repo_url()` used before `apps` existed.
        """
        from agentit.portal.store import AssessmentStore

        store1 = await make_store()  # truncated fresh by the shared fixture
        r1 = make_report(repo_name="legacy-app")
        aid1 = await store1.save(r1)
        await store1.set_infra_repo_url(aid1, "https://github.com/org/legacy-infra")

        # Simulate a pre-`apps`-table database: drop the row `save()`/
        # `set_infra_repo_url()` already wrote, so the next `create()`'s
        # SCHEMA_SQL backfill has real history to reconstruct from.
        await store1._pool.execute("DELETE FROM apps")

        store2 = await AssessmentStore.create(postgres_dsn, min_size=1, max_size=2)
        try:
            assert await store2._last_known_infra_repo_url(
                "https://github.com/org/legacy-app"
            ) == "https://github.com/org/legacy-infra"
        finally:
            await store2.close()

    async def test_apps_backfill_is_a_no_op_once_already_populated(self, postgres_dsn):
        """The `WHERE repo_url NOT IN (SELECT repo_url FROM apps)` guard
        must not let a second `create()`/backfill pass clobber a row a
        caller has since updated (e.g. re-registered with a new URL)."""
        from agentit.portal.store import AssessmentStore

        store1 = await make_store()
        aid = await store1.save(make_report(repo_name="reopen-app"))
        await store1.set_infra_repo_url(aid, "https://github.com/org/first-infra")

        store2 = await AssessmentStore.create(postgres_dsn, min_size=1, max_size=2)
        try:
            assert await store2._last_known_infra_repo_url(
                "https://github.com/org/reopen-app"
            ) == "https://github.com/org/first-infra"
        finally:
            await store2.close()


# ── Severity enum regression ───────────────────────────────────────────


async def test_fleet_data_counts_critical_findings_correctly():
    """Regression: severity comparison must use Severity enum, not raw ints."""
    store = await make_store()
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
    await store.save(report)
    fleet = await store.get_fleet_data()
    assert len(fleet) == 1
    assert fleet[0]["critical_count"] == 2  # 1 critical + 1 high


# ── Agent Registry ─────────────────────────────────────────────────────


class TestAgentRegistryTable:
    async def test_register_and_list(self):
        store = await make_store()
        aid = await store.register_agent("security", "hardening", "network,rbac")
        agents = await store.list_agents()
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "security"
        assert agents[0]["category"] == "hardening"
        assert agents[0]["status"] == "active"

    async def test_heartbeat(self):
        store = await make_store()
        await store.register_agent("observability", "monitoring")
        assert await store.agent_heartbeat("observability") is True

    async def test_heartbeat_upserts_unregistered_agent(self):
        """Long-lived watchers (vuln-watcher, slo-tracker, ...) never call
        register_agent -- agent_heartbeat must create the row itself so their
        "last seen" actually shows up on the Agents/Schedules pages."""
        store = await make_store()
        assert await store.agent_heartbeat("vuln-watcher") is True
        agents = await store.list_agents()
        assert any(a["agent_name"] == "vuln-watcher" for a in agents)
        watcher = next(a for a in agents if a["agent_name"] == "vuln-watcher")
        assert watcher["category"] == "watcher"
        assert watcher["last_heartbeat"] is not None

    async def test_register_replaces_existing(self):
        store = await make_store()
        await store.register_agent("security", "hardening", "v1")
        await store.register_agent("security", "hardening", "v2")
        agents = await store.list_agents()
        assert len(agents) == 1
        assert agents[0]["capabilities"] == "v2"


class TestPruneStaleAgents:
    """`prune_stale_agents()` -- removes agent_registry rows for agents no
    longer known to the codebase (e.g. the 9 Python agents removed in favor
    of skills-only generation)."""

    async def test_removes_names_outside_known_set(self):
        store = await make_store()
        await store.register_agent("security", "hardening")
        await store.register_agent("cost", "cost")

        pruned = await store.prune_stale_agents(known_names={"cost"})

        assert pruned == ["security"]
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"cost"}

    async def test_preserves_all_known_names(self):
        store = await make_store()
        known = {"codechange", "vuln-watcher",
                  "slo-tracker", "drift-detector", "skill-learner"}
        for name in known:
            await store.register_agent(name, "test")

        pruned = await store.prune_stale_agents(known_names=known)

        assert pruned == []
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == known

    async def test_no_stale_rows_returns_empty_list(self):
        store = await make_store()
        await store.register_agent("cost", "cost")
        assert await store.prune_stale_agents(known_names={"cost"}) == []

    async def test_prunes_multiple_stale_rows_at_once(self):
        store = await make_store()
        for name in ("chaos", "cicd", "compliance", "hardening", "incident",
                     "infrastructure", "observability", "release", "retirement"):
            await store.register_agent(name, name)
        await store.register_agent("codechange", "codechange")

        known = {"codechange", "vuln-watcher",
                  "slo-tracker", "drift-detector", "skill-learner"}
        pruned = await store.prune_stale_agents(known_names=known)

        assert len(pruned) == 9
        remaining = {a["agent_name"] for a in await store.list_agents()}
        assert remaining == {"codechange"}

    async def test_heartbeat_only_watcher_also_pruned_if_unknown(self):
        """Rows created via agent_heartbeat() (never register_agent()) are
        pruned the same way as rows created via register_agent()."""
        store = await make_store()
        await store.agent_heartbeat("some-removed-watcher")
        pruned = await store.prune_stale_agents(known_names={"vuln-watcher"})
        assert pruned == ["some-removed-watcher"]


# ── SLOs ───────────────────────────────────────────────────────────────


class TestSlosTable:
    async def test_save_and_list(self):
        store = await make_store()
        aid = await store.save(make_report())
        sid = await store.save_slo(aid, "availability", 99.9)
        slos = await store.list_slos(aid)
        assert len(slos) == 1
        assert slos[0]["metric_name"] == "availability"
        assert slos[0]["target_value"] == 99.9
        assert slos[0]["status"] == "unknown"
        assert slos[0]["id"] == sid

    async def test_update_slo(self):
        store = await make_store()
        aid = await store.save(make_report())
        sid = await store.save_slo(aid, "error_rate", 0.1)
        assert await store.update_slo(sid, 0.05, "met") is True
        slos = await store.list_slos(aid)
        assert slos[0]["current_value"] == 0.05
        assert slos[0]["status"] == "met"
        assert slos[0]["updated_at"] is not None

    async def test_multiple_slos_per_assessment(self):
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_slo(aid, "availability", 99.9)
        await store.save_slo(aid, "latency_p99", 200.0)
        await store.save_slo(aid, "error_rate", 0.1)
        slos = await store.list_slos(aid)
        assert len(slos) == 3

    async def test_delete(self):
        store = await make_store()
        aid = await store.save(make_report())
        sid = await store.save_slo(aid, "availability", 99.9)
        assert await store.delete_slo(sid, aid) is True
        assert await store.list_slos(aid) == []

    async def test_delete_wrong_assessment_returns_false(self):
        store = await make_store()
        aid = await store.save(make_report())
        other_aid = await store.save(make_report(repo_name="other-app"))
        sid = await store.save_slo(aid, "availability", 99.9)
        assert await store.delete_slo(sid, other_aid) is False
        assert len(await store.list_slos(aid)) == 1

    async def test_save_slo_still_allows_multiple_rows_per_metric_name(self):
        """`save_slo()` itself stays a plain insert -- the Add-SLO form and
        the progress-bar-direction test both rely on being able to track
        more than one threshold for the same metric_name on one
        assessment. Seeding dedup lives in
        `FleetOrchestrator._create_default_slos()`; `list_slos()` only
        collapses identical ``(metric_name, target_value)`` pairs across
        *different* assessments (see
        ``test_list_slos_collapses_identical_rows_across_reassessments``).
        """
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_slo(aid, "availability", 99.5)
        await store.save_slo(aid, "availability", 99.9)

        assert len(await store.list_slos(aid)) == 2

    async def test_list_slos_keeps_same_identity_rows_on_one_assessment(self):
        """Same ``(metric_name, target_value)`` twice on one assessment
        (progress-bar direction fixture) must not be collapsed — only
        cross-assessment historical copies are deduped."""
        store = await make_store()
        aid = await store.save(make_report(repo_name="slo-same-assessment"))
        a = await store.save_slo(aid, "error_rate", 0.5)
        b = await store.save_slo(aid, "error_rate", 0.5)
        await store.update_slo(a, 0.05, "met")
        await store.update_slo(b, 5.0, "breached")

        slos = await store.list_slos(aid)
        assert len(slos) == 2
        assert {s["id"] for s in slos} == {a, b}

    async def test_list_slos_collapses_identical_rows_across_reassessments(self):
        """Fleet SLOs regression: before default-SLO seeding skipped
        existing metrics, each re-onboard inserted another full set of
        the same ``(metric_name, target_value)`` under a new
        assessment_id. ``list_slos()`` is repo_url-scoped, so those
        historical copies all surfaced — N of each metric on
        ``/fleet/slos``. Keep only the newest assessment's copy.
        """
        store = await make_store()
        assessment_ids: list[str] = []
        for month in (1, 2, 3):
            report = make_report(repo_name="slo-triple-app")
            report.assessed_at = datetime(2025, month, 1, tzinfo=timezone.utc)
            aid = await store.save(report)
            assessment_ids.append(aid)
            await store.save_slo(aid, "availability", 99.9)
            await store.save_slo(aid, "error_rate", 1.0)

        raw_count = await store._pool.fetchval(
            "SELECT COUNT(*) FROM slos WHERE assessment_id = ANY($1::text[])",
            assessment_ids,
        )
        assert raw_count == 6

        slos = await store.list_slos(assessment_ids[-1])
        assert len(slos) == 2
        assert sorted(s["metric_name"] for s in slos) == ["availability", "error_rate"]
        assert {s["assessment_id"] for s in slos} == {assessment_ids[-1]}
        assert {s["target_value"] for s in slos} == {99.9, 1.0}

    async def test_slo_from_old_assessment_visible_after_reassessment(self):
        """Orphaned-SLO-attribution regression, same shape as gates: an SLO
        created against an app's OLD assessment_id (typically at onboarding
        time, via `FleetOrchestrator._create_default_slos()`) must still be
        visible from `list_slos()` called with that SAME app's CURRENT
        (re-assessed) assessment_id -- `slos.assessment_id` is a FK to
        whichever assessment existed when the SLO was created, but
        `get_fleet_data()`/`fleet_slos()` always key off the latest one.
        """
        store = await make_store()
        old_report = make_report(repo_name="slo-repo-x")
        old_report.assessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        old_id = await store.save(old_report)
        slo_id = await store.save_slo(old_id, "availability", 99.9)

        new_report = make_report(repo_name="slo-repo-x")
        new_report.assessed_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
        new_id = await store.save(new_report)
        assert new_id != old_id

        # Old exact-match behavior (pre-fix) would return [] here.
        slos = await store.list_slos(new_id)
        assert [s["id"] for s in slos] == [slo_id]

    async def test_delete_slo_from_old_assessment_via_current_assessment_id(self):
        """An SLO made visible on the current assessment's SLOs page purely
        because it was carried forward (see the test above) must also be
        deletable from that same page -- `delete_slo()` gets the identical
        repo_url-scoping fix as `list_slos()`, not just an exact
        `assessment_id` match on the SLO's own (older) FK value."""
        store = await make_store()
        old_id = await store.save(make_report(repo_name="slo-repo-y"))
        slo_id = await store.save_slo(old_id, "availability", 99.9)
        new_id = await store.save(make_report(repo_name="slo-repo-y"))

        assert await store.delete_slo(slo_id, new_id) is True
        assert await store.list_slos(new_id) == []

    async def test_delete_slo_for_a_different_app_still_returns_false(self):
        """The repo_url-based scoping fix must not widen `delete_slo()` into
        deleting SLOs that genuinely belong to a different app."""
        store = await make_store()
        aid = await store.save(make_report(repo_name="slo-repo-mine"))
        other_aid = await store.save(make_report(repo_name="slo-repo-other"))
        slo_id = await store.save_slo(aid, "availability", 99.9)

        assert await store.delete_slo(slo_id, other_aid) is False
        assert len(await store.list_slos(aid)) == 1


# ── Orchestrator wiring ────────────────────────────────────────────────


class TestOrchestratorStoreWiring:
    async def test_files_generated_recorded_on_onboard(self):
        """Orchestrator records each agent's real generated-file output on
        its own `AgentResult` (`files_generated`) when assessment_id is
        provided -- the removed `remediations` table used to additionally
        persist a parallel, hand-maintained copy of this same fact with no
        link to the fix's real delivery/PR outcome (see
        `FleetOrchestrator._record_remediations()`'s removal); the
        agent_results/agent_runs the orchestrator already produces are the
        real, non-fabricated record now.

        criticality="high" so dependency/cost/codechange are actually
        planned -- security/observability/cicd/compliance are now
        skill-only domains (see docs/agent-removal-readiness.md).
        """
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = await make_store()
        async_store = store
        report = make_report(criticality="high")
        aid = await store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=async_store, assessment_id=aid,
            )
            result = await orch.run()

        agent_names_with_files = {r.agent_name for r in result.agent_results if r.files_generated}
        assert agent_names_with_files & {"skills", "codechange"}

    async def test_agents_registered_on_run(self):
        """Orchestrator registers available Python agents in the store.

        Only codechange remains as a registered Python onboarding agent.
        """
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = await make_store()
        async_store = store
        report = make_report(criticality="high")
        aid = await store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=async_store, assessment_id=aid,
            )
            await orch.run()

        agents = await store.list_agents()
        agent_names = {a["agent_name"] for a in agents}
        assert "codechange" in agent_names, "codechange not registered"

    async def test_run_succeeds_without_assessment_id(self):
        """Orchestrator still runs (and generates files) when assessment_id
        is None -- e.g. a dry-run/preview invocation with nothing persisted
        yet. criticality="high" so dependency/cost/codechange are actually
        planned -- see the sibling test above for why."""
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = await make_store()
        async_store = store
        report = make_report(criticality="high")
        await store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=async_store,
            )
            result = await orch.run()

        assert any(r.success for r in result.agent_results)

    async def test_slos_created_on_onboard(self):
        """Orchestrator creates default SLOs after release agent runs."""
        from agentit.agents.orchestrator import FleetOrchestrator
        import tempfile
        from pathlib import Path

        store = await make_store()
        async_store = store
        report = make_report()
        aid = await store.save(report)

        with tempfile.TemporaryDirectory() as tmpdir:
            orch = FleetOrchestrator(
                report=report, output_dir=Path(tmpdir),
                store=async_store, assessment_id=aid,
            )
            await orch.run()

        slos = await store.list_slos(aid)
        assert len(slos) == 3
        metric_names = {s["metric_name"] for s in slos}
        assert "availability" in metric_names
        assert "error_rate" in metric_names
        assert "latency_p99_ms" in metric_names


# ── Onboarding history ────────────────────────────────────────────────


# ── Apply results (repo_files_json migration) ──────────────────────────


class TestApplyResultsTable:
    async def test_save_and_get_apply_results_fresh_db(self):
        """Regression: a brand-new DB must create apply_results with
        repo_files_json already in the CREATE TABLE statement, so
        save_apply_results (which always writes that column) doesn't fail
        with 'table apply_results has no column named repo_files_json'."""
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_apply_results(
            aid,
            {"applied": ["a.yaml"], "skipped": [], "errors": [], "repo_files": ["a.yaml"]},
            namespace="test-ns",
            dry_run=False,
        )
        result = await store.get_apply_results(aid)
        assert result is not None
        assert result["applied"] == ["a.yaml"]
        assert result["repo_files"] == ["a.yaml"]
        assert result["namespace"] == "test-ns"

    async def test_create_is_idempotent_when_called_again_against_the_same_database(self, postgres_dsn):
        """Regression, Postgres-shaped: re-running `AssessmentStore.create()`
        (SCHEMA_SQL's `CREATE TABLE IF NOT EXISTS`/`ADD COLUMN IF NOT
        EXISTS`) against a database that already has every column/table
        (e.g. a second replica pod, or a restart) must not raise -- unlike
        the SQLite-era version of this test, there is no hand-rolled
        try/except OperationalError migration dance to regress here;
        Postgres's own `IF NOT EXISTS` clauses make this natively safe, and
        this test is the regression guard that keeps it that way."""
        from agentit.portal.store import AssessmentStore

        store1 = await make_store()
        aid = await store1.save(make_report())
        await store1.save_apply_results(
            aid, {"applied": [], "skipped": [], "errors": [], "repo_files": []},
            namespace="ns", dry_run=True,
        )

        store2 = await AssessmentStore.create(postgres_dsn, min_size=1, max_size=2)  # must not raise
        try:
            result = await store2.get_apply_results(aid)
            assert result is not None
        finally:
            await store2.close()


class TestOnboardingHistory:
    async def test_list_onboardings(self):
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_onboarding(aid, [
            {"category": "security", "path": "rbac.yaml", "content": "x", "description": "d"},
        ], orchestration={"recommendation": "READY", "auto_approve": False})
        await store.save_onboarding(aid, [
            {"category": "security", "path": "rbac.yaml", "content": "x", "description": "d"},
            {"category": "cicd", "path": "pipeline.yaml", "content": "y", "description": "p"},
        ], orchestration={"recommendation": "AUTO-APPROVED", "auto_approve": True})

        history = await store.list_onboardings(aid)
        assert len(history) == 2
        assert history[0]["file_count"] == 2  # most recent first
        assert history[1]["file_count"] == 1

    async def test_list_onboardings_empty(self):
        store = await make_store()
        aid = await store.save(make_report())
        assert await store.list_onboardings(aid) == []


# ── Fleet-wide feedback (Insights page) ──────────────────────────────────


class TestGetAllFeedback:
    async def test_get_all_feedback_returns_feedback_across_apps(self):
        """Regression: the now-deleted get_feedback_for_app("") filtered on
        app_name = '' and always returned nothing -- get_all_feedback is the
        fleet-wide fix."""
        store = await make_store()
        await store.record_feedback("app-a", "security", "network-policy", "approved")
        await store.record_feedback("app-b", "compliance", "sbom", "rejected", human_reason="not needed")

        feedback = await store.get_all_feedback()
        assert len(feedback) == 2
        assert {f["app_name"] for f in feedback} == {"app-a", "app-b"}

    async def test_get_all_feedback_respects_limit_and_order(self):
        store = await make_store()
        for i in range(5):
            await store.record_feedback(f"app-{i}", "security", "cat", "approved")
        feedback = await store.get_all_feedback(limit=3)
        assert len(feedback) == 3
        # most recent first
        assert feedback[0]["app_name"] == "app-4"


class TestFleetWideRejectionStats:
    """get_fleet_wide_rejection_stats() -- capability-scout's fleet-wide
    aggregate (docs/self-improvement-for-agentit.md), unlike
    get_rejection_count() which is scoped to one app + one category."""

    async def test_aggregates_rejection_rate_per_category_across_apps(self):
        store = await make_store()
        await store.record_feedback("app-a", "security", "network-policy", "rejected")
        await store.record_feedback("app-b", "security", "network-policy", "rejected")
        await store.record_feedback("app-c", "security", "network-policy", "approved")
        await store.record_feedback("app-a", "compliance", "sbom", "approved")

        stats = await store.get_fleet_wide_rejection_stats()
        by_category = {s["finding_category"]: s for s in stats}
        assert by_category["network-policy"]["total"] == 3
        assert by_category["network-policy"]["rejected"] == 2
        assert by_category["network-policy"]["rejection_rate"] == round(2 / 3 * 100, 1)
        assert by_category["sbom"]["rejected"] == 0

    async def test_sorted_by_rejected_count_descending(self):
        store = await make_store()
        await store.record_feedback("app-a", "security", "low-rejects", "rejected")
        for _ in range(3):
            await store.record_feedback("app-a", "security", "high-rejects", "rejected")

        stats = await store.get_fleet_wide_rejection_stats()
        assert stats[0]["finding_category"] == "high-rejects"

    async def test_respects_limit(self):
        store = await make_store()
        for i in range(5):
            await store.record_feedback("app-a", "security", f"cat-{i}", "rejected")
        assert len(await store.get_fleet_wide_rejection_stats(limit=2)) == 2

    async def test_empty_when_no_feedback(self):
        store = await make_store()
        assert await store.get_fleet_wide_rejection_stats() == []


class TestGetEvent:
    """get_event() -- single-row lookup by primary key, backing the
    Self-Improvement tab's per-run drill-through page."""

    async def test_returns_the_matching_event(self):
        store = await make_store()
        eid = await store.log_event("capability-scout", "capability-run", None, "info", "proposed something")
        event = await store.get_event(eid)
        assert event is not None
        assert event["id"] == eid
        assert event["summary"] == "proposed something"

    async def test_returns_none_for_unknown_id(self):
        store = await make_store()
        assert await store.get_event("does-not-exist") is None


# ── Dead-letter queue republish ──────────────────────────────────────────


class TestRetryDlqMessage:
    async def test_retry_republishes_to_original_topic(self):
        store = await make_store()
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

    async def test_retry_falls_back_to_relabel_when_no_original_topic(self):
        """Rows written before original_topic tracking existed (or a Kafka
        failure) must still mark the row retried instead of silently no-op'ing."""
        store = await make_store()
        eid = await store.log_event(
            "event-consumer", "dead-letter", "my-app", "error", "old-style row",
            details={"original_message": {"foo": "bar"}, "error": "boom"},
        )
        assert await store.retry_dlq_message(eid) is True
        events = await store.list_events(limit=10)
        retry_event = next(e for e in events if e["action"] == "dlq-retry")
        assert "relabelled only" in retry_event["summary"]

    async def test_retry_unknown_event_returns_false(self):
        store = await make_store()
        assert await store.retry_dlq_message("nonexistent") is False


# ── Agent Runs ────────────────────────────────────────────────────────────


class TestAgentRuns:
    async def test_save_and_list_agent_runs(self):
        store = await make_store()
        await store.save_agent_run("security", "local", "success", duration_ms=1200, resource_tier="standard")
        await store.save_agent_run("security", "local", "error", duration_ms=50, error="boom")

        runs = await store.list_agent_runs("security")
        assert len(runs) == 2
        assert runs[0]["status"] == "error"  # most recent first
        assert runs[1]["status"] == "success"
        assert runs[1]["duration_ms"] == 1200

    async def test_get_agent_stats_uses_agent_runs_not_events(self):
        """Regression: get_agent_stats previously LIKE-matched event `action`
        strings ('%complete%'/'%failed%'), which double-counted unrelated
        events. It must now be derived purely from agent_runs."""
        store = await make_store()
        # An unrelated event containing "complete" must not be counted.
        await store.log_event("security", "onboarding-complete", "app", "info", "noise")
        await store.save_agent_run("security", "local", "success", duration_ms=100)
        await store.save_agent_run("security", "local", "success", duration_ms=200)
        await store.save_agent_run("security", "local", "error", duration_ms=50)

        stats = await store.get_agent_stats("security")
        assert len(stats) == 1
        assert stats[0]["total_events"] == 3
        assert stats[0]["successes"] == 2
        assert stats[0]["failures"] == 1
        assert stats[0]["success_rate"] == round(2 / 3 * 100, 1)

# ── Check Result Snapshots ────────────────────────────────────────────────


class TestCheckResults:
    async def test_save_and_get_check_compliance(self):
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_check_results(aid, [
            {"check_name": "has-network-policy", "dimension": "security", "passed": True},
            {"check_name": "has-network-policy", "dimension": "security", "passed": False},
            {"check_name": "has-sbom", "dimension": "compliance", "passed": True},
        ])

        compliance = await store.get_check_compliance()
        by_name = {c["check_name"]: c for c in compliance}
        assert by_name["has-network-policy"]["passes"] == 1
        assert by_name["has-network-policy"]["total"] == 2
        assert by_name["has-network-policy"]["pass_rate"] == 50.0
        assert by_name["has-sbom"]["pass_rate"] == 100.0

    async def test_save_check_results_noop_on_empty_list(self):
        store = await make_store()
        aid = await store.save(make_report())
        await store.save_check_results(aid, [])
        assert await store.get_check_compliance() == []


# ── Correlation IDs ───────────────────────────────────────────────────────


class TestCorrelationId:
    async def test_log_event_persists_correlation_id(self):
        store = await make_store()
        await store.log_event("orchestrator", "completed", "app", "info", "done", correlation_id="chain-1")
        await store.log_event("orchestrator", "completed", "app", "info", "unrelated", correlation_id="chain-2")

        chain = await store.list_events_by_correlation_id("chain-1")
        assert len(chain) == 1
        assert chain[0]["summary"] == "done"

    async def test_save_sets_correlation_id_to_assessment_id(self):
        store = await make_store()
        aid = await store.save(make_report())
        chain = await store.list_events_by_correlation_id(aid)
        assert len(chain) == 1
        assert chain[0]["action"] == "assessment-complete"


# ── DB stats / metrics ────────────────────────────────────────────────────


# Row-count + size_bytes coverage for get_db_stats() lives in
# tests/test_store.py's TestDbStats -- the SQLite-era versions here
# (":memory: has no file size" / a real on-disk file's size) were
# inherently SQLite-file-shaped assumptions that don't translate to a
# connection-pool-backed Postgres store (see store.py's get_db_stats()
# docstring: `pg_database_size()` is the real equivalent).
