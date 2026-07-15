"""Tests for the skill learner watcher — the automatic counterpart to the
manual `agentit learn` CLI command and the portal's learn button."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from click.testing import CliRunner

from agentit.cli import main
from agentit.watchers.skill_learner import SkillLearner
from conftest import make_async_store


def _learner(**kwargs) -> tuple[SkillLearner, MagicMock]:
    publisher = MagicMock()
    learner = SkillLearner(publisher=publisher, **kwargs)
    return learner, publisher


async def test_research_once_generates_new_skill():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00001"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=False), \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: cve-2099-00001\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/cve-2099-00001.md")):
        saved, skipped = await learner.research_once()

    assert saved == ["cve-2099-00001"]
    assert skipped == []
    publisher.publish.assert_called_once()
    _, kwargs = publisher.publish.call_args
    assert kwargs["action"] == "skills-generated"
    assert "cve-2099-00001" in kwargs["summary"]


async def test_research_once_skips_existing_skill():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00002"}]), \
         patch("agentit.learning_agent.check_skill_exists", return_value=True):
        saved, skipped = await learner.research_once()

    assert saved == []
    assert skipped == ["CVE-2099-00002"]
    publisher.publish.assert_not_called()


async def test_research_once_no_llm_returns_empty_without_raising():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
        saved, skipped = await learner.research_once()

    assert saved == []
    assert skipped == []
    publisher.publish.assert_not_called()


async def test_research_once_no_llm_logs_learning_run_event_when_store_present():
    """Every tick must leave a durable trace -- including the LLM-unavailable
    case, which previously logged nothing to the store at all (only a
    stderr echo, invisible once the pod's logs roll over)."""
    async_store, store = make_async_store()
    learner, _ = _learner(store=async_store)
    with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
        await learner.research_once()

    events = store.list_events_by_action("learning-run")
    assert len(events) == 1
    assert events[0]["agent_id"] == "skill-learner"
    assert events[0]["severity"] == "error"
    assert "no credentials" in events[0]["summary"]


async def test_research_once_no_research_results():
    learner, publisher = _learner()
    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[]):
        saved, skipped = await learner.research_once()

    assert saved == []
    assert skipped == []
    publisher.publish.assert_not_called()


async def test_research_once_prioritizes_flagged_skill_over_cve_sweep(tmp_path):
    """Regression: the research cycle must check get_low_effectiveness_skills()
    first and research a replacement for the flagged skill instead of the
    generic CVE sweep -- this is the wiring that closes the self-improvement
    loop end to end."""
    skills_dir = tmp_path / "skills"
    (skills_dir / "security").mkdir(parents=True)
    (skills_dir / "security" / "network-policy.md").write_text(
        "---\nname: network-policy\ndomain: security\nversion: 1\n"
        "triggers: [network]\noutputs: [NetworkPolicy]\nstatus: active\n---\nbody\n",
        encoding="utf-8",
    )

    async_store, store = make_async_store()
    for _ in range(4):
        store.record_skill_outcome("network-policy", "app-a", "rejected", "wrong")
    store.record_skill_outcome("network-policy", "app-b", "rejected", "wrong")

    learner, publisher = _learner(store=async_store, skills_dir=skills_dir)

    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves") as mock_cves, \
         patch("agentit.learning_agent.research_skill_improvement",
               return_value={"title": "network-policy-v2", "description": "better"}) as mock_improve, \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: network-policy-v2\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/network-policy-v2.md")):
        saved, skipped = await learner.research_once()

    mock_improve.assert_called_once()
    args, _ = mock_improve.call_args
    assert args[1] == "network-policy"
    assert args[2] == "security"
    mock_cves.assert_not_called()
    assert saved == ["network-policy-v2"]
    assert skipped == []

    events = store.list_events()
    assert any(e["action"] == "skill-improvement-drafted" for e in events)
    learning_runs = store.list_events_by_action("learning-run")
    assert len(learning_runs) == 1
    assert learning_runs[0]["severity"] == "info"


async def test_research_once_falls_back_to_cve_sweep_when_nothing_flagged():
    """No low-effectiveness skills -> the existing CVE-sweep behavior runs
    exactly as before."""
    async_store, store = make_async_store()
    learner, publisher = _learner(store=async_store)

    with patch("agentit.llm.LLMClient", return_value=object()), \
         patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00009"}]) as mock_cves, \
         patch("agentit.learning_agent.research_skill_improvement") as mock_improve, \
         patch("agentit.learning_agent.check_skill_exists", return_value=False), \
         patch("agentit.learning_agent.generate_skill_from_research",
               return_value="---\nname: cve-2099-00009\n---\nbody"), \
         patch("agentit.learning_agent.save_skill",
               return_value=Path("/tmp/fake-skills/security/cve-2099-00009.md")):
        saved, skipped = await learner.research_once()

    mock_improve.assert_not_called()
    mock_cves.assert_called_once()
    assert saved == ["cve-2099-00009"]

    learning_runs = store.list_events_by_action("learning-run")
    assert len(learning_runs) == 1
    assert learning_runs[0]["agent_id"] == "skill-learner"
    assert learning_runs[0]["severity"] == "info"
    assert "cve-2099-00009" in learning_runs[0]["summary"]


def test_learn_watch_cli_options_registered():
    runner = CliRunner()
    result = runner.invoke(main, ["learn-watch", "--help"])
    assert result.exit_code == 0
    assert "--interval" in result.output
    assert "--limit" in result.output


def test_accepts_optional_store_for_tick_telemetry():
    async_store, _raw = make_async_store()
    learner, _ = _learner(store=async_store)
    assert learner._store is async_store


def test_defaults_to_none_store_when_omitted():
    learner, _ = _learner()
    assert learner._store is None


class TestAsyncRunLoop:
    """Phase 3 (docs/postgres-migration-plan.md §9): run() became async def,
    with time.sleep() -> await asyncio.sleep()."""

    @patch("agentit.watchers.skill_learner.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_run_ticks_once_then_stops_on_interrupt(self, mock_sleep, capsys):
        learner, _ = _learner()
        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
            await learner.run()

        captured = capsys.readouterr()
        assert "Starting skill learner" in captured.err
        assert "Skill learner stopped." in captured.err
        mock_sleep.assert_called_once_with(86400)


class TestTickRunsOnEventLoop:
    """``research_once`` is now a genuine coroutine -- ``run()`` awaits it
    directly rather than dispatching the whole tick to a worker thread. Its
    own blocking LLM/file-system calls are each narrowly wrapped in
    ``asyncio.to_thread`` internally, and record_tick telemetry must still
    fire afterwards."""

    @patch("agentit.watchers.skill_learner.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_research_once_awaited_directly_and_telemetry_records(self, mock_sleep):
        async_store, store = make_async_store()
        learner, _ = _learner(store=async_store)

        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
             patch.object(learner, "research_once", wraps=learner.research_once) as mock_research_once:
            await learner.run()

        mock_research_once.assert_called_once_with()
        events = store.list_events()
        assert any(e["action"] == "tick-complete" for e in events)

    async def test_llm_client_init_dispatched_via_to_thread(self):
        """The narrow-to_thread call site: LLMClient() construction (a
        synchronous LLM SDK call) must not run directly on the event loop."""
        learner, _ = _learner()

        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")), \
             patch(
                 "agentit.watchers.skill_learner.asyncio.to_thread", wraps=asyncio.to_thread
             ) as mock_to_thread:
            await learner.research_once()

        assert mock_to_thread.call_count >= 1


class TestConstructionAcceptsAsyncStoreDirectly:
    """The real bug fixed here: cli.py used to hand SkillLearner
    `store.raw` because `research_once` called every store method
    unawaited. Now the store is genuinely awaited throughout, so a store
    constructed via `create_store()`'s own facade must work end to end."""

    async def test_research_once_works_against_create_store_facade(self, tmp_path):
        from agentit.portal.store_factory import create_store

        store = await create_store(":memory:")
        learner, _ = _learner(store=store, skills_dir=tmp_path / "skills")

        with patch("agentit.llm.LLMClient", side_effect=RuntimeError("no credentials")):
            saved, skipped = await learner.research_once()  # must not raise AttributeError/TypeError

        assert saved == []
        assert skipped == []


class TestCrossPodVisibility:
    """The self-improvement-loop gap this fix closes: the watcher's own pod
    has no shared filesystem with the portal (no RWX storage class is
    available on this cluster -- see chart/templates/agents/
    skill-learner.yaml). ``_save_draft`` now pushes each draft to the
    portal's own process via an internal API call instead of only writing
    to this pod's isolated disk, falling back to local disk only if the
    portal can't be reached.
    """

    async def test_submit_draft_to_portal_returns_name_on_success(self):
        learner, _ = _learner()
        learner._client.post = AsyncMock(
            return_value=httpx.Response(200, json={"status": "saved", "name": "cve-2099-00050"})
        )

        name = await learner._submit_draft_to_portal("---\nname: cve-2099-00050\n---\nbody", "security")

        assert name == "cve-2099-00050"
        learner._client.post.assert_called_once()
        args, kwargs = learner._client.post.call_args
        assert args[0] == f"{learner._portal_url}/api/webhook/skill-draft"
        assert kwargs["json"] == {"content": "---\nname: cve-2099-00050\n---\nbody", "domain": "security"}

    async def test_submit_draft_to_portal_sends_internal_token_when_configured(self, monkeypatch):
        monkeypatch.setenv("AGENTIT_INTERNAL_WEBHOOK_TOKEN", "s3cr3t-token")
        learner, _ = _learner()
        learner._client.post = AsyncMock(return_value=httpx.Response(200, json={"name": "x"}))

        await learner._submit_draft_to_portal("content", "security")

        _, kwargs = learner._client.post.call_args
        assert kwargs["headers"]["X-Internal-Webhook-Token"] == "s3cr3t-token"

    async def test_submit_draft_to_portal_returns_none_on_non_200(self):
        learner, _ = _learner()
        learner._client.post = AsyncMock(return_value=httpx.Response(500, text="db down"))

        assert await learner._submit_draft_to_portal("content", "security") is None

    async def test_submit_draft_to_portal_returns_none_when_unreachable(self):
        """Real, unmocked failure mode -- the portal is genuinely
        unreachable (matches the established RemediationLoop test
        convention of pointing at a bad host with a short timeout)."""
        learner, _ = _learner(portal_url="http://bad-host:9999", timeout=2)

        assert await learner._submit_draft_to_portal("content", "security") is None
        await learner._client.aclose()

    async def test_submit_draft_to_portal_retries_404_then_succeeds(self):
        """Real 2026-07-15 incident: AGENTIT_PORTAL_URL points at the Argo
        Rollouts *stable* Service, which stays pinned to the old
        ReplicaSet's pods until a canary rollout fully promotes -- so this
        route can genuinely 404 for a window even though it's correctly
        wired in the code about to become stable. A 404 followed by a 200
        (the rollout finishing promotion) must resolve to the saved name,
        not a permanent PVC fallback."""
        learner, _ = _learner(draft_retry_delay=0)
        learner._client.post = AsyncMock(side_effect=[
            httpx.Response(404, text="not found"),
            httpx.Response(200, json={"name": "cve-2099-00060"}),
        ])

        name = await learner._submit_draft_to_portal("content", "security")

        assert name == "cve-2099-00060"
        assert learner._client.post.call_count == 2

    async def test_submit_draft_to_portal_gives_up_after_max_404_retries(self):
        learner, _ = _learner(draft_retry_attempts=2, draft_retry_delay=0)
        learner._client.post = AsyncMock(return_value=httpx.Response(404, text="still not found"))

        name = await learner._submit_draft_to_portal("content", "security")

        assert name is None
        assert learner._client.post.call_count == 2

    async def test_submit_draft_to_portal_does_not_retry_non_404_rejections(self):
        """A 500 (or any non-404 rejection) is a real failure, not rollout
        skew -- retrying it would just waste the tick's time budget."""
        learner, _ = _learner(draft_retry_delay=0)
        learner._client.post = AsyncMock(return_value=httpx.Response(500, text="db down"))

        name = await learner._submit_draft_to_portal("content", "security")

        assert name is None
        learner._client.post.assert_called_once()

    async def test_save_draft_prefers_portal_over_local_disk(self, tmp_path):
        learner, _ = _learner(skills_dir=tmp_path / "skills")
        learner._client.post = AsyncMock(
            return_value=httpx.Response(200, json={"name": "remote-saved"})
        )

        with patch("agentit.learning_agent.save_skill") as mock_save_local:
            name = await learner._save_draft("content", "security")

        assert name == "remote-saved"
        mock_save_local.assert_not_called()

    async def test_save_draft_falls_back_to_local_disk_when_portal_unreachable(self, tmp_path, caplog):
        learner, _ = _learner(skills_dir=tmp_path / "skills")
        learner._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        fake_path = tmp_path / "skills" / "security" / "fallback-skill.md"
        with patch("agentit.learning_agent.save_skill", return_value=fake_path) as mock_save_local:
            name = await learner._save_draft("content", "security")

        assert name == "fallback-skill"
        mock_save_local.assert_called_once_with("content", tmp_path / "skills", domain="security")
        assert any("NOT yet visible on the Capabilities page" in r.message for r in caplog.records)

    async def test_watcher_drafted_skill_visible_via_portal_skill_listing(self, tmp_path, monkeypatch):
        """End-to-end proof: a skill drafted by the watcher's own code path
        (``research_once`` -> ``_save_draft`` -> ``_submit_draft_to_portal``)
        is actually visible via the *portal's own* skill-listing logic
        (``skill_engine.load_all_skills`` -- the same function
        ``capabilities.py``'s ``_cached_skills()`` calls) -- not just "a
        file exists somewhere". Exercises the real FastAPI route
        (``routes/webhooks.py::webhook_skill_draft``) over an in-process
        ASGI transport; only the LLM/CVE-research plumbing is mocked.
        """
        from agentit.portal.app import app
        from agentit.skill_engine import load_all_skills

        monkeypatch.chdir(tmp_path)
        (tmp_path / "skills" / "security").mkdir(parents=True)

        learner, publisher = _learner()
        learner._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        )
        learner._portal_url = "http://testserver"

        with patch("agentit.llm.LLMClient", return_value=object()), \
             patch("agentit.learning_agent.research_cves", return_value=[{"id": "CVE-2099-00042"}]), \
             patch("agentit.learning_agent.check_skill_exists", return_value=False), \
             patch("agentit.learning_agent.generate_skill_from_research", return_value=(
                 "---\nname: cve-2099-00042\ndomain: security\nversion: 1\n"
                 "triggers: [test]\noutputs: [NetworkPolicy]\nstatus: draft\n---\nbody\n"
             )):
            saved, skipped = await learner.research_once()

        await learner._client.aclose()

        assert saved == ["cve-2099-00042"]
        assert skipped == []
        publisher.publish.assert_called_once()

        skills = load_all_skills(tmp_path / "skills")
        assert any(s.name == "cve-2099-00042" and s.status == "draft" for s in skills)
