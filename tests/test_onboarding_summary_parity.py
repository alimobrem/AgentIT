"""Regression tests: helpers.run_onboarding (webhook path) and
services/onboard_pipeline.py's _run_onboarding (portal onboard-job path,
moved out of routes/assessments.py 2026-07-20) must produce orchestration
summaries with the same fields, so the two call paths can never drift
apart on what gets persisted.

`auto_approve`/`gates` used to be two of those shared fields (persisted
alongside every onboarding run as orchestrator risk-tier data) -- both were
deleted 2026-07-20 (`OrchestrationPlan.auto_approve`/`_can_auto_approve()`/
`_determine_gates()`/`OrchestrationResult.gates_created` all removed from
`agents/orchestrator.py`) once an architecture-review audit confirmed their
only real consumer, AutoMode, was fully removed and nothing else read them
(`list_onboardings()`'s own `orch.get("auto_approve", False)` readback
already degraded gracefully with no live template ever rendering it). The
parity guarantee this file tests is unaffected: both paths still delegate
to the exact same shared implementation, so whatever fields it produces
(`agents`/`conflicts`/`recommendation` today) can never drift between them.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agentit.portal.helpers import run_onboarding
from conftest import make_async_store, make_report, make_store


@contextmanager
def _isolated_helpers_store(async_store):
    """run_onboarding() resolves its store via helpers.get_store(), backed by
    a module-level singleton -- redirect that to an isolated test store (like
    conftest's portal_client fixture does) so tests never touch/lock the real
    on-disk DB."""
    with patch("agentit.portal.helpers.get_store", return_value=async_store), \
         patch("agentit.portal.helpers._store", async_store):
        yield


async def test_run_onboarding_summary_has_expected_shape(tmp_path: Path):
    async_store, store = await make_async_store()
    report = make_report(criticality="low")
    aid = await store.save(report)

    with _isolated_helpers_store(async_store):
        files, orch_summary = await run_onboarding(report, assessment_id=aid)

    # Fields shared with the display/history code path.
    assert "conflicts" in orch_summary
    assert isinstance(orch_summary["conflicts"], list)
    assert "recommendation" in orch_summary
    assert isinstance(orch_summary["recommendation"], str)
    assert "agents" in orch_summary
    for agent_entry in orch_summary["agents"]:
        assert "files_count" in agent_entry
    # auto_approve/gates were deleted 2026-07-20 -- must never reappear.
    assert "auto_approve" not in orch_summary
    assert "gates" not in orch_summary


async def test_app_py_delegates_to_shared_run_onboarding():
    """services/onboard_pipeline.py's _run_onboarding must be the same
    shared implementation used by the webhook path, so the two can never
    drift on which summary fields get stored (the original bug this file
    guards against: the portal onboard-job path included fields helpers.py
    silently omitted)."""
    from agentit.portal.services import onboard_pipeline

    async_store, store = await make_async_store()
    report = make_report(criticality="low")
    aid = await store.save(report)

    # services/onboard_pipeline.py's _run_onboarding is now genuinely async
    # (FleetOrchestrator is async throughout) -- its caller resolves
    # the async get_store() itself and passes that store in explicitly --
    # verify _run_onboarding forwards it unchanged.
    files, orch_summary = await onboard_pipeline._run_onboarding(report, aid, async_store)

    assert "conflicts" in orch_summary
    assert "recommendation" in orch_summary
    assert "agents" in orch_summary


async def test_run_onboarding_uses_explicitly_passed_store_not_the_singleton():
    """Regression: run_onboarding(store=...) must use the store it's given,
    not silently fall back to the module singleton -- this is exactly what
    broke test isolation for app.py's onboarding route after the fields
    were unified into one shared helper (events must land in the isolated
    test store, never the real on-disk agentit.db)."""
    async_store, store = await make_async_store()
    report = make_report(criticality="low")
    aid = await store.save(report)

    await run_onboarding(report, assessment_id=aid, store=async_store)

    events = await store.list_events()
    assert len(events) > 0  # orchestrator agents logged events into OUR store


async def test_orchestration_summary_persists_real_recommendation_value():
    """End-to-end regression for the original bug this file guards against
    (a field the orchestrator genuinely computed getting silently dropped
    or hardcoded on the way to storage): the persisted orchestration
    summary must carry the SAME `recommendation` value the orchestrator
    computed, not an omitted/default one. (Originally written against
    `auto_approve`, deleted 2026-07-20 -- `recommendation` is the
    replacement real, dynamically-computed field that still exercises the
    same round-trip.)"""
    async_store, store = await make_async_store()
    report = make_report(criticality="low")
    aid = await store.save(report)

    with _isolated_helpers_store(async_store):
        files, orch_summary = await run_onboarding(report, assessment_id=aid)
    await store.save_onboarding(aid, files, orchestration=orch_summary)

    stored_orch = await store.get_orchestration(aid)
    assert stored_orch["recommendation"] == orch_summary["recommendation"]
    assert stored_orch["recommendation"] != ""
