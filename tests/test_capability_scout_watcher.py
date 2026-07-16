"""Tests for the capability-scout watcher (agentit.watchers.capability_scout)
-- the automatic loop that proposes small, evidence-grounded changes to
AgentIT's own codebase. Mirrors tests/test_skill_learner.py's structure;
see tests/test_capability_scout.py for the pure gate/evidence logic this
watcher calls into."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agentit.cli import main
from agentit.watchers.capability_scout import CapabilityScout
from conftest import make_async_store


def _scout(**kwargs) -> tuple[CapabilityScout, MagicMock]:
    publisher = MagicMock()
    scout = CapabilityScout(publisher=publisher, **kwargs)
    return scout, publisher


def _proposal(**overrides) -> dict:
    base = {
        "has_proposal": True,
        "title": "Track stack signatures",
        "gap_description": "README documents an idea that was never built",
        "evidence": "README.md:42 — Documented future idea (not built)",
        "target_files": ["src/agentit/portal/store.py", "tests/test_store.py"],
        "change_summary": "Add a counter and a threshold check",
        "risk": "low",
        "test_plan": "Assert the threshold logic in a new test",
    }
    base.update(overrides)
    return base


async def test_research_once_no_llm_logs_error_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    scout, publisher = _scout(store=async_store)

    with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
        result = await scout.research_once()

    assert result == {"outcome": "no-llm"}
    events = await store.list_events_by_action("capability-run")
    assert len(events) == 1
    assert events[0]["severity"] == "error"
    assert "no credentials" in events[0]["summary"]
    publisher.publish.assert_not_called()


async def test_research_once_insufficient_signal_logs_no_signal_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    scout, publisher = _scout(store=async_store)

    with patch("agentit.llm.LLMClient", return_value=object()):
        result = await scout.research_once()

    assert result == {"outcome": "no-signal"}
    events = await store.list_events_by_action("capability-run")
    assert len(events) == 1
    assert events[0]["severity"] == "warning"
    publisher.publish.assert_not_called()


async def test_research_once_llm_declines_to_propose(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    for i in range(6):
        await store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
    scout, publisher = _scout(store=async_store)

    mock_llm = MagicMock()
    mock_llm.propose_capability_improvement.return_value = {"has_proposal": False}
    with patch("agentit.llm.LLMClient", return_value=mock_llm):
        result = await scout.research_once()

    assert result == {"outcome": "no-proposal"}
    events = await store.list_events_by_action("capability-run")
    assert len(events) == 1
    publisher.publish.assert_not_called()


async def test_research_once_unparseable_proposal_is_parse_error(tmp_path, monkeypatch):
    """None from propose_capability_improvement must not look like an honest
    'nothing to propose' — that hid live JSON/fence truncation failures."""
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    for i in range(6):
        await store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
    scout, publisher = _scout(store=async_store)

    mock_llm = MagicMock()
    mock_llm.propose_capability_improvement.return_value = None
    with patch("agentit.llm.LLMClient", return_value=mock_llm):
        result = await scout.research_once()

    assert result == {"outcome": "parse-error"}
    events = await store.list_events_by_action("capability-run")
    assert len(events) == 1
    assert events[0]["severity"] == "error"
    assert "unparseable" in events[0]["summary"].lower() or "unparseable" in str(events[0].get("details")).lower()
    publisher.publish.assert_not_called()


async def test_research_once_gate_blocked_when_no_test_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    for i in range(6):
        await store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
    scout, publisher = _scout(store=async_store)

    mock_llm = MagicMock()
    mock_llm.propose_capability_improvement.return_value = _proposal(test_plan="")
    with patch("agentit.llm.LLMClient", return_value=mock_llm), \
         patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
         patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")):
        result = await scout.research_once()

    assert result == {"outcome": "gate-blocked"}
    events = await store.list_events_by_action("capability-run")
    assert len(events) == 1
    assert events[0]["severity"] == "warning"
    publisher.publish.assert_not_called()


async def test_research_once_opens_pr_and_logs_proposed_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    for i in range(6):
        await store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
    scout, publisher = _scout(store=async_store, repo_dir=tmp_path)

    mock_llm = MagicMock()
    mock_llm.propose_capability_improvement.return_value = _proposal()
    with patch("agentit.llm.LLMClient", return_value=mock_llm), \
         patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
         patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")), \
         patch("agentit.git_pr.create_branch_commit_push", return_value={"success": True, "branch": "b"}), \
         patch("agentit.git_pr.open_draft_pr", return_value={"pr_url": "https://github.com/org/agentit/pull/9"}):
        result = await scout.research_once()

    assert result == {"outcome": "proposed", "pr_url": "https://github.com/org/agentit/pull/9", "build_mode": "docs"}
    events = await store.list_events_by_action("capability-run")
    assert len(events) == 1
    assert events[0]["severity"] == "info"
    publisher.publish.assert_called_once()
    _, kwargs = publisher.publish.call_args
    assert kwargs["action"] == "capability-proposed"

    # The proposal doc must actually be written to disk before pushing.
    written = tmp_path / "docs" / "proposals" / "track-stack-signatures.md"
    assert written.is_file()


async def test_research_once_pr_creation_failure_logs_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async_store, store = await make_async_store()
    for i in range(6):
        await store.record_feedback(f"app-{i}", "hardening", f"cat-{i}", "rejected")
    scout, publisher = _scout(store=async_store, repo_dir=tmp_path)

    mock_llm = MagicMock()
    mock_llm.propose_capability_improvement.return_value = _proposal()
    with patch("agentit.llm.LLMClient", return_value=mock_llm), \
         patch("agentit.capability_scout.check_no_open_self_improve_pr", return_value=(True, "ok")), \
         patch("agentit.capability_scout.run_test_suite", return_value=(True, "ok")), \
         patch("agentit.git_pr.create_branch_commit_push", return_value={"success": False, "error": "push rejected"}):
        result = await scout.research_once()

    assert result == {"outcome": "pr-failed"}
    events = await store.list_events_by_action("capability-run")
    assert events[0]["severity"] == "error"
    publisher.publish.assert_not_called()


def test_propose_watch_cli_options_registered():
    runner = CliRunner()
    result = runner.invoke(main, ["propose-watch", "--help"])
    assert result.exit_code == 0
    assert "--interval" in result.output
    assert "--max-open-prs" in result.output


async def test_accepts_optional_store_for_tick_telemetry():
    async_store, _raw = await make_async_store()
    scout, _ = _scout(store=async_store)
    assert scout._store is async_store


def test_defaults_to_none_store_when_omitted():
    scout, _ = _scout()
    assert scout._store is None


class TestAsyncRunLoop:
    async def test_run_ticks_once_then_stops_on_interrupt(self, capsys):
        # Skip startup grace so the first sleep_with_heartbeat is the tick interval.
        scout, _ = _scout(startup_grace_seconds=0)
        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
             patch("agentit.watchers.sleep_with_heartbeat", side_effect=KeyboardInterrupt) as mock_sleep:
            await scout.run()

        captured = capsys.readouterr()
        assert "Starting capability-scout" in captured.err
        assert "capability-scout stopped." in captured.err
        mock_sleep.assert_called_once_with(86400)

    async def test_startup_grace_interrupt_stops_gracefully(self, capsys):
        """A KeyboardInterrupt during the startup-grace sleep (the real
        default -- startup_grace_seconds=90) must be caught the same way
        the steady-state loop's is, not propagate out of run() uncaught.
        Regression test: the startup-grace sleep used to be unwrapped, so
        this would previously raise instead of stopping gracefully."""
        scout, _ = _scout()  # default startup_grace_seconds=90
        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
             patch("agentit.watchers.sleep_with_heartbeat", side_effect=KeyboardInterrupt) as mock_sleep:
            await scout.run()

        captured = capsys.readouterr()
        assert "Startup grace" in captured.err
        assert "capability-scout stopped." in captured.err
        # Never reached the tick loop -- the interrupt fired during the
        # grace period's own sleep_with_heartbeat(90) call.
        mock_sleep.assert_called_once_with(90)


class TestTickRunsOnEventLoopAndRecordsTelemetry:
    async def test_research_once_awaited_directly_and_telemetry_records(self):
        async_store, store = await make_async_store()
        scout, _ = _scout(store=async_store, startup_grace_seconds=0)

        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
             patch("agentit.watchers.sleep_with_heartbeat", side_effect=KeyboardInterrupt), \
             patch.object(scout, "research_once", wraps=scout.research_once) as mock_research_once:
            await scout.run()

        mock_research_once.assert_called_once_with()
        events = await store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)
