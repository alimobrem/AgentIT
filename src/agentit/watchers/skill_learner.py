"""Skill learner agent — periodically researches CVEs via LLM and drafts new skills.

This is the automatic counterpart to the manual `agentit learn` CLI command
and the portal's "Research CVEs & Generate Skills" button on the Capabilities
page. All three call the same `agentit.learning_agent` functions; this one
just runs on an interval instead of waiting for a human or a click.

Generated skills are always saved with `status: draft` (set by the LLM
itself, per the prompt in `learning_agent.generate_skill_from_research`) and
require human review via `agentit activate-skill` before the skill engine
will match them against real assessments — this loop drafts, it never
auto-activates.

**Cross-pod visibility.** This watcher runs in its own pod, separate from
the portal, with no shared filesystem between them (no ReadWriteMany
storage class is available on this cluster -- confirmed via `oc get
storageclass`: only `gp2-csi`/`gp3-csi`, both EBS-backed and
ReadWriteOnce-only -- so a shared/RWX PVC was ruled out as the fix). Every
draft this watcher generates is therefore pushed straight to the portal via
an internal-token-authenticated API call
(`POST /api/webhook/skill-draft`, `routes/webhooks.py`) -- the same
`AGENTIT_PORTAL_URL` + `AGENTIT_INTERNAL_WEBHOOK_TOKEN` pattern
`RemediationLoop` already uses to call back into the portal from a
separate watcher pod. That endpoint calls the exact same `save_skill()`
the portal's own in-process "Research CVEs & Generate Skills" button
uses, into the portal's own `skills/` tree, and busts its 60s skills
cache -- so a watcher-drafted skill is visible on the Capabilities page
on the very next page load, no restart or manual sync step needed.

If the portal can't be reached that cycle (network blip, mid-rollout,
etc.), the draft falls back to this pod's own dedicated PVC
(`AGENTIT_SKILLS_DIR`, `chart/templates/agents/skill-learner.yaml`, gated
behind `agents.skillLearner.persistence.enabled`, default on) so nothing
is lost -- `_save_draft` logs a loud warning in that (expected to be rare)
case, since a draft that only exists on that PVC stays invisible until a
human recovers it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import click

from agentit.events import EventPublisher
from agentit.internal_webhook_client import internal_webhook_client
from agentit.watchers import record_tick, sleep_with_heartbeat

logger = logging.getLogger(__name__)

DEFAULT_PORTAL = os.environ.get("AGENTIT_PORTAL_URL", "http://localhost:8080")


class SkillLearner:
    """Long-lived agent that periodically researches recent CVEs via the LLM
    and drafts new skills for any that aren't covered yet.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        llm_model: str | None = None,
        interval: int = 86400,
        limit: int = 3,
        skills_dir: Path | None = None,
        store: object | None = None,
        portal_url: str = DEFAULT_PORTAL,
        timeout: int = 30,
        draft_retry_attempts: int = 3,
        draft_retry_delay: int = 20,
        startup_grace_seconds: int = 120,
        startup_probe_interval: int = 10,
    ) -> None:
        self._publisher = publisher
        self._llm_model = llm_model
        self._interval = interval
        self._limit = limit
        self._skills_dir = skills_dir or Path("skills")
        self._store = store
        self._portal_url = portal_url.rstrip("/")
        # `internal_webhook_client` attaches the X-Internal-Webhook-Token
        # header once, at construction -- the same shared helper
        # `RemediationLoop` uses for the identical "separate watcher pod
        # calling back into the portal" problem, so there's structurally
        # only one way to make this call correctly.
        self._client = internal_webhook_client(timeout=timeout)
        self._draft_retry_attempts = draft_retry_attempts
        self._draft_retry_delay = draft_retry_delay
        self._startup_grace_seconds = startup_grace_seconds
        self._startup_probe_interval = startup_probe_interval

    async def research_once(self) -> tuple[list[str], list[str]]:
        """Research low-effectiveness skills first, falling back to a generic
        CVE sweep when nothing's flagged. Returns (saved, skipped).

        This is the wiring that actually closes the self-improvement loop:
        before this, the research cycle only ever swept CVEs on its own
        schedule, blind to which of its own already-shipped skills humans
        keep rejecting (``skill_effectiveness``, populated by
        ``skill_engine.record_skill_outcomes`` from the real onboarding
        apply/gate/auto-mode paths -- see ``docs/postgres-migration-plan.md``
        and this repo's README for the full loop). A skill flagged low this
        cycle is prioritized; CVE research only runs when there's nothing to
        improve.

        ``self._store`` is the async-compatible store handed in by
        ``cli.py``'s ``learn_watch`` command -- every store call is
        `await`ed directly. The LLM/file-system calls (synchronous by
        design, per docs/postgres-migration-plan.md's narrow-``to_thread``
        convention -- ``llm.py`` itself is deliberately not converted, see
        that doc's "sync→async conversion ... complete" section) are each
        wrapped in ``asyncio.to_thread`` at their own call site.
        """
        try:
            from agentit.llm import LLMClient
            llm_client = await asyncio.to_thread(LLMClient, model=self._llm_model)
        except Exception as exc:
            click.echo(f"[skill-learn] LLM unavailable this cycle: {exc}", err=True)
            await self._log_run(mode=None, saved=[], skipped=[], error=str(exc))
            return [], []

        from agentit.learning_agent import (
            check_skill_exists,
            generate_skill_from_research,
            research_cves,
            research_skill_improvement,
        )

        saved: list[str] = []
        skipped: list[str] = []

        flagged = await self._get_flagged_skills()
        if flagged:
            click.echo(f"[skill-learn] {len(flagged)} low-effectiveness skill(s) flagged -- "
                       "prioritizing improvement research over the generic CVE sweep this cycle.", err=True)
            from agentit.skill_engine import load_all_skills
            all_skills = await asyncio.to_thread(load_all_skills, self._skills_dir)
            by_name = {s.name: s for s in all_skills}

            for entry in flagged[: self._limit]:
                skill_name = entry["skill"]
                skill = by_name.get(skill_name)
                if skill is None:
                    click.echo(f"[skill-learn] Skipping improvement research for "
                               f"'{skill_name}' -- skill no longer found on disk", err=True)
                    skipped.append(skill_name)
                    continue
                item = await asyncio.to_thread(research_skill_improvement, llm_client, skill.name, skill.domain, entry)
                if not item:
                    skipped.append(skill_name)
                    continue
                content = await asyncio.to_thread(generate_skill_from_research, llm_client, item, domain=skill.domain)
                if not content:
                    skipped.append(skill_name)
                    continue
                # Deliberately skip check_skill_exists() here -- the point
                # is a replacement for an existing (underperforming) skill,
                # so a name/domain match against that same skill is
                # expected, not a duplicate to reject.
                name = await self._save_draft(content, skill.domain)
                if name:
                    saved.append(name)
                    click.echo(f"[skill-learn] Drafted improvement for '{skill_name}': {name}", err=True)
                    if self._store is not None:
                        try:
                            await self._store.log_event(
                                "skill-learner", "skill-improvement-drafted", None, "info",
                                f"Drafted {name} to improve low-effectiveness skill "
                                f"'{skill_name}' ({entry.get('approval_rate', 0):.0%} approval)",
                            )
                        except Exception:
                            logger.warning("Failed to log skill-improvement-drafted event", exc_info=True)

            if saved or skipped:
                if saved:
                    self._publisher.publish(
                        "agentit-events",
                        agent_id="skill-learner",
                        action="skills-generated",
                        target_app=None,
                        severity="info",
                        summary=f"Drafted {len(saved)} skill improvement(s) for review: {', '.join(saved)}",
                    )
                else:
                    click.echo("[skill-learn] No usable improvement drafts this cycle", err=True)
                await self._log_run(mode="skill-improvement", saved=saved, skipped=skipped)
                return saved, skipped

        items = await asyncio.to_thread(research_cves, llm_client, limit=self._limit)
        click.echo(f"[skill-learn] Researched {len(items)} CVE(s)", err=True)

        for item in items:
            item_name = item.get("id") or item.get("title") or item.get("name", "")
            if item_name and await asyncio.to_thread(check_skill_exists, self._skills_dir, item_name, "security"):
                skipped.append(item_name)
                continue
            content = await asyncio.to_thread(generate_skill_from_research, llm_client, item, domain="security")
            if not content:
                continue
            name = await self._save_draft(content, "security")
            if name:
                saved.append(name)
                click.echo(f"[skill-learn] Drafted new skill: {name}", err=True)

        if saved:
            self._publisher.publish(
                "agentit-events",
                agent_id="skill-learner",
                action="skills-generated",
                target_app=None,
                severity="info",
                summary=f"Drafted {len(saved)} new skill(s) for review: {', '.join(saved)}",
            )
        else:
            click.echo("[skill-learn] No new skills this cycle", err=True)

        await self._log_run(mode="cve-sweep", saved=saved, skipped=skipped)
        return saved, skipped

    async def _save_draft(self, content: str, domain: str) -> str | None:
        """Persist one drafted skill so it's visible on the portal's
        Capabilities page immediately -- no shared filesystem, portal
        restart, or manual sync step required.

        Primary path: push the draft to the portal's own process via
        ``_submit_draft_to_portal`` (an internal-API call), which writes it
        into the exact `skills/` tree the portal's own in-process
        "Research CVEs & Generate Skills" button already uses. Falls back
        to this pod's own ``self._skills_dir`` (the dedicated PVC, when
        mounted) only if the portal couldn't be reached -- see this
        module's docstring for why that fallback is expected to be rare,
        not the normal case.
        """
        name = await self._submit_draft_to_portal(content, domain)
        if name:
            return name

        from agentit.learning_agent import save_skill
        path = await asyncio.to_thread(save_skill, content, self._skills_dir, domain=domain)
        if path is None:
            return None
        logger.warning(
            "Portal unreachable this cycle -- draft '%s' saved to this pod's own "
            "%s only and is NOT yet visible on the Capabilities page until "
            "manually recovered.", path.stem, self._skills_dir,
        )
        return path.stem

    async def _submit_draft_to_portal(self, content: str, domain: str) -> str | None:
        """POST a drafted skill to ``/api/webhook/skill-draft`` so it lands
        in the portal's own process instead of this pod's isolated
        filesystem. ``self._client`` already carries the
        ``X-Internal-Webhook-Token`` header from construction (via the
        shared ``internal_webhook_client`` helper -- the same one
        ``RemediationLoop`` uses for the same "separate watcher pod calling
        back into the portal" problem). Returns the saved skill's name, or
        ``None`` if the portal couldn't be reached or rejected the draft
        this cycle.

        Retries specifically on HTTP 404 (not other statuses): confirmed
        root cause of a real incident is that ``AGENTIT_PORTAL_URL`` points
        at the Argo Rollouts *stable* Service, whose selector only flips
        over to the new ReplicaSet once a canary rollout fully promotes
        (chart uses ``strategy.canary`` with ``stableService: agentit`` /
        ``canaryService: agentit-canary``) -- so mid-rollout, this route can
        genuinely 404 against the still-serving old pod for as long as the
        canary takes to promote, even though the route exists and is
        correctly wired in the code that's *about* to become stable. Live
        cluster reproduction: `oc exec` into the stable-hash pod curling
        this exact path returned 404 while the canary-hash pod behind the
        same Service returned 401 (i.e. route present, just unauthenticated)
        for the identical request. A short retry window usually outlasts
        that skew instead of silently losing the draft to the PVC fallback
        in ``_save_draft`` below.
        """
        for attempt in range(1, self._draft_retry_attempts + 1):
            try:
                resp = await self._client.post(
                    f"{self._portal_url}/api/webhook/skill-draft",
                    json={"content": content, "domain": domain},
                )
            except Exception as exc:
                logger.warning(
                    "Could not reach portal at %s to submit skill draft: %s", self._portal_url, exc,
                )
                return None

            if resp.status_code == 200:
                return resp.json().get("name")

            if resp.status_code == 404 and attempt < self._draft_retry_attempts:
                logger.warning(
                    "Portal 404'd skill draft submission (attempt %d/%d) -- likely "
                    "mid-rollout version skew against the stable Service; retrying "
                    "in %ds: %s",
                    attempt, self._draft_retry_attempts, self._draft_retry_delay, resp.text[:200],
                )
                await asyncio.sleep(self._draft_retry_delay)
                continue

            logger.warning(
                "Portal rejected skill draft submission (HTTP %s): %s",
                resp.status_code, resp.text[:200],
            )
            return None
        return None

    async def _log_run(
        self, mode: str | None, saved: list[str], skipped: list[str], error: str | None = None,
    ) -> None:
        """Durable, queryable trace of this tick's outcome -- see
        ``learning_agent.describe_learning_run``'s docstring for why this
        exists (every run, not just ones that generated a skill, must leave
        a trace). Best-effort: a store failure must never crash the watcher's
        main loop, same convention as ``watchers/__init__.py::record_tick``.
        """
        if self._store is None:
            return
        from agentit.learning_agent import LEARNING_RUN_ACTION, describe_learning_run

        severity, summary, details = describe_learning_run("watcher", mode, saved, skipped, error)
        try:
            await self._store.log_event(
                "skill-learner", LEARNING_RUN_ACTION, None, severity, summary, details=details,
            )
        except Exception:
            logger.warning("Failed to log learning-run event", exc_info=True)

    async def _get_flagged_skills(self) -> list[dict]:
        """Low-effectiveness skills from the store, or ``[]`` if there's no
        store (e.g. ``agentit learn`` CLI invocations without a DB path) or
        the lookup fails -- never lets this new prioritization block the
        existing CVE-sweep fallback."""
        if self._store is None or not hasattr(self._store, "get_low_effectiveness_skills"):
            return []
        try:
            return await self._store.get_low_effectiveness_skills()
        except Exception:
            logger.warning("Failed to fetch low-effectiveness skills", exc_info=True)
            return []

    async def _wait_for_portal_draft_route(self) -> bool:
        """Block the first research tick until ``/api/webhook/skill-draft`` is
        reachable as something other than HTML 404.

        Root cause of a repeated live incident: this watcher and the portal
        roll out together under Argo Rollouts canary. ``AGENTIT_PORTAL_URL``
        points at the *stable* Service, which stays on the old ReplicaSet
        until the canary fully promotes -- so a brand-new skill-learner pod
        can POST drafts into an old portal that 404s the route for minutes,
        silently parking CVE skills on the learner PVC only. Probing with
        GET (expects 405 Method Not Allowed once the route exists, or 401
        if token auth is required for GET-less paths) avoids that first-tick
        race. Proceeds after ``startup_grace_seconds`` even if still 404 so
        a genuinely missing route doesn't hang the watcher forever.
        """
        import time

        url = f"{self._portal_url}/api/webhook/skill-draft"
        deadline = time.monotonic() + self._startup_grace_seconds
        click.echo(
            f"[skill-learn] Waiting up to {self._startup_grace_seconds}s for portal "
            f"skill-draft route at {url} (avoids mid-canary 404s)...",
            err=True,
        )
        while time.monotonic() < deadline:
            Path("/tmp/heartbeat").touch()
            try:
                resp = await self._client.get(url)
                if resp.status_code != 404:
                    click.echo(
                        f"[skill-learn] Portal skill-draft route ready (HTTP {resp.status_code}).",
                        err=True,
                    )
                    return True
            except Exception as exc:
                logger.warning("Portal skill-draft probe failed: %s", exc)
            await asyncio.sleep(self._startup_probe_interval)
        click.echo(
            f"[skill-learn] Portal skill-draft route still 404 after "
            f"{self._startup_grace_seconds}s — proceeding anyway (per-draft retries still apply).",
            err=True,
        )
        return False

    async def run(self) -> None:
        """Main loop: research, sleep.

        ``research_once`` is now a genuine coroutine -- it's `await`ed
        directly rather than dispatched via ``asyncio.to_thread`` (which
        would just add a redundant thread hop), since its own blocking
        LLM/file-system calls are already narrowly wrapped in
        ``asyncio.to_thread`` internally.

        Sleeps between ticks via ``watchers.sleep_with_heartbeat`` (the same
        helper ``vuln_watcher.py`` uses), touching ``/tmp/heartbeat`` every
        ``HEARTBEAT_REFRESH_SECONDS`` instead of only once per full
        ``--interval`` (86400s/24h default) -- a plain ``asyncio.sleep``
        here left the liveness probe's staleness check stale for up to 24h,
        which previously had to be papered over by loosening the probe's
        threshold to 172800s (chart/templates/agents/skill-learner.yaml) --
        see this fix's history for why that threshold is back down to a
        real value now that the heartbeat is genuinely kept fresh.
        """
        click.echo(f"Starting skill learner (interval={self._interval}s)...", err=True)
        click.echo(
            f"[skill-learn] Drafts are submitted to the portal at {self._portal_url} "
            "(POST /api/webhook/skill-draft) so they show up on the Capabilities page "
            "immediately, with no restart or manual sync needed. If the portal can't "
            f"be reached that cycle, drafts fall back to this pod's own {self._skills_dir} "
            "and a warning is logged -- that's the only case where a draft stays invisible.",
            err=True,
        )
        try:
            await self._wait_for_portal_draft_route()
        except KeyboardInterrupt:
            click.echo("Skill learner stopped.", err=True)
            await self._client.aclose()
            return
        while True:
            try:
                await self.research_once()
                Path("/tmp/heartbeat").touch()
                await record_tick(self._store, "skill-learner", success=True)
            except KeyboardInterrupt:
                click.echo("Skill learner stopped.", err=True)
                break
            except Exception as exc:
                logger.exception("skill-learn tick failed")
                click.echo(f"[skill-learn] Error: {exc}", err=True)
                await record_tick(self._store, "skill-learner", success=False, error=str(exc))

            try:
                await sleep_with_heartbeat(self._interval)
            except KeyboardInterrupt:
                click.echo("Skill learner stopped.", err=True)
                break
        await self._client.aclose()
