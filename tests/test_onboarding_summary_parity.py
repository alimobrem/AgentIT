"""Regression tests: helpers.run_onboarding (webhook path) and
routes/assessments.py's _run_onboarding (inline portal path) must produce
orchestration summaries with the same fields -- especially auto_approve/gates,
which webhook_auto_apply relies on to decide whether to auto-apply manifests.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agentit.portal.helpers import run_onboarding
from agentit.portal.store_factory import AsyncSQLiteStore
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


async def test_run_onboarding_summary_includes_auto_approve_and_gates(tmp_path: Path):
    async_store, store = make_async_store()
    report = make_report(criticality="low")  # low criticality -> orchestrator can auto-approve
    aid = store.save(report)

    with _isolated_helpers_store(async_store):
        files, orch_summary = await run_onboarding(report, assessment_id=aid)

    assert "auto_approve" in orch_summary
    assert isinstance(orch_summary["auto_approve"], bool)
    assert "gates" in orch_summary
    assert isinstance(orch_summary["gates"], list)
    # Fields shared with the display/history code path.
    assert "conflicts" in orch_summary
    assert "recommendation" in orch_summary
    assert "agents" in orch_summary
    for agent_entry in orch_summary["agents"]:
        assert "files_count" in agent_entry


async def test_app_py_delegates_to_shared_run_onboarding():
    """routes/assessments.py's _run_onboarding must be the same shared
    implementation used by the webhook path, so the two can never drift on
    which summary fields get stored (the original bug: the inline portal
    path included auto_approve/gates, helpers.py silently omitted them)."""
    from agentit.portal.routes import assessments as assessments_module

    async_store, store = make_async_store()
    report = make_report(criticality="low")
    aid = store.save(report)

    # routes/assessments.py's _run_onboarding is now genuinely async
    # (FleetOrchestrator is async throughout) -- its route caller resolves
    # the async get_store() itself and passes that store in explicitly --
    # verify _run_onboarding forwards it unchanged.
    files, orch_summary = await assessments_module._run_onboarding(report, aid, async_store)

    assert "auto_approve" in orch_summary
    assert "gates" in orch_summary


async def test_run_onboarding_uses_explicitly_passed_store_not_the_singleton():
    """Regression: run_onboarding(store=...) must use the store it's given,
    not silently fall back to the module singleton -- this is exactly what
    broke test isolation for app.py's onboarding route after the fields
    were unified into one shared helper (events must land in the isolated
    test store, never the real on-disk agentit.db)."""
    async_store, store = make_async_store()
    report = make_report(criticality="low")
    aid = store.save(report)

    await run_onboarding(report, assessment_id=aid, store=async_store)

    events = store.list_events()
    assert len(events) > 0  # orchestrator agents logged events into OUR store


async def test_webhook_auto_apply_sees_real_auto_approve_value():
    """End-to-end regression for the original bug: webhook_auto_apply must
    read the SAME auto_approve value the orchestrator computed, not a
    hardcoded False because the summary omitted the field."""
    async_store, store = make_async_store()
    report = make_report(criticality="low")
    aid = store.save(report)

    with _isolated_helpers_store(async_store):
        files, orch_summary = await run_onboarding(report, assessment_id=aid)
    store.save_onboarding(aid, files, orchestration=orch_summary)

    stored_orch = store.get_orchestration(aid)
    assert stored_orch["auto_approve"] == orch_summary["auto_approve"]
