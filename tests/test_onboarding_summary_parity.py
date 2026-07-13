"""Regression tests: helpers.run_onboarding (webhook path) and app.py's
_run_onboarding (inline portal path) must produce orchestration summaries
with the same fields -- especially auto_approve/gates, which
webhook_auto_apply relies on to decide whether to auto-apply manifests.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agentit.portal.helpers import run_onboarding
from conftest import make_report, make_store


@contextmanager
def _isolated_helpers_store(store):
    """run_onboarding() resolves its store via helpers.get_store(), backed by
    a module-level singleton -- redirect that to an isolated test store (like
    conftest's portal_client fixture does) so tests never touch/lock the real
    on-disk DB."""
    with patch("agentit.portal.helpers.get_store", return_value=store), \
         patch("agentit.portal.helpers._store", store):
        yield


def test_run_onboarding_summary_includes_auto_approve_and_gates(tmp_path: Path):
    store = make_store()
    report = make_report(criticality="low")  # low criticality -> orchestrator can auto-approve
    aid = store.save(report)

    with _isolated_helpers_store(store):
        files, orch_summary = run_onboarding(report, assessment_id=aid)

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


def test_app_py_delegates_to_shared_run_onboarding():
    """app.py's _run_onboarding must be the same shared implementation used
    by the webhook path, so the two can never drift on which summary fields
    get stored (the original bug: app.py included auto_approve/gates,
    helpers.py silently omitted them)."""
    from agentit.portal import app as app_module

    store = make_store()
    report = make_report(criticality="low")
    aid = store.save(report)

    # app.py's _run_onboarding resolves its store via app.py's OWN
    # get_store() (passed explicitly into helpers.run_onboarding) so that
    # `patch("agentit.portal.app.get_store", ...)` overrides -- as used by
    # test_portal.py's _override_store fixture -- keep working correctly.
    with patch("agentit.portal.app.get_store", return_value=store):
        files, orch_summary = app_module._run_onboarding(report, aid)

    assert "auto_approve" in orch_summary
    assert "gates" in orch_summary


def test_run_onboarding_uses_explicitly_passed_store_not_the_singleton():
    """Regression: run_onboarding(store=...) must use the store it's given,
    not silently fall back to the module singleton -- this is exactly what
    broke test isolation for app.py's onboarding route after the fields
    were unified into one shared helper (events must land in the isolated
    test store, never the real on-disk agentit.db)."""
    store = make_store()
    report = make_report(criticality="low")
    aid = store.save(report)

    run_onboarding(report, assessment_id=aid, store=store)

    events = store.list_events()
    assert len(events) > 0  # orchestrator agents logged events into OUR store


def test_webhook_auto_apply_sees_real_auto_approve_value():
    """End-to-end regression for the original bug: webhook_auto_apply must
    read the SAME auto_approve value the orchestrator computed, not a
    hardcoded False because the summary omitted the field."""
    store = make_store()
    report = make_report(criticality="low")
    aid = store.save(report)

    with _isolated_helpers_store(store):
        files, orch_summary = run_onboarding(report, assessment_id=aid)
    store.save_onboarding(aid, files, orchestration=orch_summary)

    stored_orch = store.get_orchestration(aid)
    assert stored_orch["auto_approve"] == orch_summary["auto_approve"]
